import logging
import numpy as np
from base64 import b64encode, b64decode
import cv2
import torch
from DCGAN import DCGAN, DropBlockNoise
from Preprocessing import preprocess, segment, dilate, get_contours_v2, draw_points
from Classifier import adjust_pretrained_weights, input_shape, NUM_CLASSES, BACKBONES, backbone
from tensorflow.keras.layers import (
    Dense, Flatten, Conv2D, Activation, BatchNormalization,
    MaxPooling2D, AveragePooling2D, GlobalAveragePooling2D, Lambda,
    Dropout, Input, concatenate, add, Conv2DTranspose,
    SpatialDropout2D, Cropping2D, UpSampling2D, LeakyReLU,
    ZeroPadding2D, Reshape, Concatenate, Multiply, Permute, Add
)
from tensorflow.keras.models import Model

from skimage.filters import gaussian
from flask import Flask, request, make_response, jsonify
from flask_cors import CORS, cross_origin

import tensorflow as tf
import matplotlib.pyplot as plt

import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

logger = logging.getLogger('app')
logger.setLevel(logging.INFO)

IMAGE_SIZE = 224
WHITE = (255, 255, 255)


def get_extraction_model():
    model = torch.hub.load(
        './yolov5', 'custom', path='./yolov5/runs/train/exp/weights/best.pt', source='local')
    return model


extraction_model = get_extraction_model()

model = DCGAN(input_shape=(IMAGE_SIZE, IMAGE_SIZE, 1),
              architecture='two-stage',
              output_activation='sigmoid',
              noise=DropBlockNoise(rate=0.1, block_size=16),
              pretrain_weights=None,
              block_type='pixel-shuffle',
              kernel_initializer='glorot_uniform',
              C=1.)
restoration_model = model.generator
restoration_model.load_weights(
    "./weights_gae/gan_efficientunet_full_augment-hist_equal_generator.h5")
restoration_model.trainable = False


app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config['CORS_HEADERS'] = 'Content-Type'

inputs = Input(shape=(224, 224, 3))
img_in = Lambda(lambda x: (x[..., 0]), name='img')(inputs)
restore_in = Lambda(lambda x: x[..., 2], name='restore')(inputs)

img_ft = backbone(img_in)
restore_ft = backbone(restore_in)

img_ft = BatchNormalization()(img_ft)
img_ft = Activation('swish')(img_ft)

restore_ft = BatchNormalization()(restore_ft)
restore_ft = Activation('swish')(restore_ft)

x = tf.abs(img_ft - restore_ft)
x = Conv2D(1024, 3, padding='same')(x)
x = BatchNormalization()(x)
x = Activation('swish')(x)

img_ft = Conv2D(1024, 3, padding='same')(img_ft)
img_ft = BatchNormalization()(img_ft)
img_ft = Activation('swish')(img_ft)

gpooling = concatenate([img_ft, x])
gpooling = GlobalAveragePooling2D()(gpooling)
gpooling = Dropout(0.2)(gpooling)
output = Dense(NUM_CLASSES, activation='softmax',
               kernel_regularizer='l2')(gpooling)

MODEL_NAME = 'convnext_base'
classifier = Model(inputs, output, name=MODEL_NAME)

classifier.load_weights('./weights_3cls/convnext_base_best_loss.h5')
classifier.trainable = False


def classify(image):
    return classifier.predict(np.expand_dims(image, axis=0)).squeeze()


@app.route('/predict', methods=['POST'])
@cross_origin()
def predict():
    logger.info(f'Request received: {request.json["image"][-10:]}')

    if 'crop' not in request.json:
        request.json['crop'] = 'false'
    else:
        request.json['crop'] = request.json['crop'].lower()
    
    try:
        img_bytes = b64decode(request.json['image'])  # get image bytes
        original = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), -1)

        if request.json['crop'] == 'true':
            results = extraction_model(original)
            bboxes = results.pandas().xyxy[0].values.tolist()

            if len(bboxes) == 0:
                logger.info('No object detected!')
                return make_response(jsonify({'error': 'No object detected'}), 400)
            coordinates = []
            for object in bboxes:
                coordinates.append([int(object[0]), int(
                    object[1]), int(object[2]), int(object[3])])

            x1, y1, x2, y2 = coordinates[0][0:4]
            cropped_image = original[y1:y2, x1:x2, ...]
            cropped_image = cv2.resize(cropped_image, (224, 224))

            cropped_str = b64encode(cv2.imencode(
                '.png', cropped_image)[1]).decode()

            original = cropped_image
            img_bytes = b64decode(cropped_str)

        # convert to grayscale if necessary
        if len(original.shape) == 3 and original.shape[2] == 3:
            original = cv2.cvtColor(original, cv2.COLOR_BGR2GRAY)
        original = preprocess(cv2.resize(original, (IMAGE_SIZE, IMAGE_SIZE), cv2.INTER_NEAREST))
    except Exception as e:
        logger.error(e)
        return make_response(jsonify({'error': 'Invalid input format'}), 400)
    
    # perform segmentation
    try:
        segmented_img = cv2.resize(segment(img_bytes, combine=True), (IMAGE_SIZE, IMAGE_SIZE), cv2.INTER_NEAREST)
        segmented_str = b64encode(cv2.imencode('.png', segmented_img)[1]).decode()
    except Exception as e:
        logger.error(e)
        return make_response(jsonify({'error': 'Segmentation failed'}), 500)

    # get contours
    try:
        uc, lc = get_contours_v2(segment(img_bytes, combine=False), verbose=0)
        mask = draw_points(np.zeros((640, 640)).astype('uint8'), lc, thickness=1, color=WHITE)
        mask = draw_points(mask, uc, thickness=1, color=WHITE)
        mask = cv2.cvtColor(cv2.resize(mask, (IMAGE_SIZE, IMAGE_SIZE), cv2.INTER_NEAREST), cv2.COLOR_BGR2GRAY) / 255.
        ok, buffer = cv2.imencode(".png", (mask * 255).astype('uint8'))
        if ok:
            contour_str = b64encode(buffer).decode()
    except Exception as e:
        logger.error(e)
        return make_response(jsonify({'error': 'Contour extraction failed'}), 500)

    # create masked image
    try:
        mask = 1 - mask
        dilated = dilate(mask)
        ok, buffer = cv2.imencode(".png", (dilated * 255).astype('uint8'))
        if ok:
            dilated_str = b64encode(buffer).decode()
        
        blurred = gaussian(dilated, sigma=50, truncate=0.3)
        ok, buffer = cv2.imencode(".png", (blurred * 255).astype('uint8'))
        if ok:
            blurred_str = b64encode(buffer).decode()

        masked_img = original * (1 - blurred)
        ok, buffer = cv2.imencode(".png", (masked_img * 255).astype('uint8'))
        if ok:
            masked_str = b64encode(buffer).decode()
    except Exception as e:
        logger.error(e)
        return make_response(jsonify({'error': 'Masking failed'}), 500)

    # restore masked image using generator
    try:
        input = tf.convert_to_tensor(np.expand_dims(masked_img, axis=0), dtype=tf.float32)
        restored_img = restoration_model(input)
        restored_img = tf.squeeze(tf.squeeze(restored_img, axis=-1), axis=0)
        ok, buffer = cv2.imencode(".png", (restored_img * 255).numpy().astype('uint8'))
        if ok:
            restored_str = b64encode(buffer).decode()
    except Exception as e:
        logger.error(e)
        return make_response(jsonify({'error': 'Restoration failed'}), 500)

    # evaluate anomaly map
    try:
        anomaly_map = blurred * tf.abs(original - restored_img)
        plt.imsave('anomaly.png', anomaly_map, cmap='turbo', vmax=0.7)
        anomaly_str = b64encode(open('anomaly.png', 'rb').read()).decode()
        os.remove('anomaly.png')
    except Exception as e:
        logger.error(e)
        return make_response(jsonify({'error': 'Anomaly map evaluation failed'}), 500)
    
    # classify the restored image
    try:
        probabilities = classify(np.stack((original, original, restored_img), axis=-1))
    except Exception as e:
        logger.error(e)
        return make_response(jsonify({'error': 'Classification failed'}), 500)
    
    logger.info('Request processed successfully!')
    if request.json['crop'] == 'true':
        return jsonify({
            'images': {
                'cropped': cropped_str,
                'segmented': segmented_str,
                'contour': contour_str,
                'dilated': dilated_str,
                'blurred': blurred_str,
                'masked': masked_str,
                'restored': restored_str,
                'anomaly': anomaly_str,
            },
            'probabilities': probabilities.tolist(),
        })
    return jsonify({
        'images': {
            'segmented': segmented_str,
            'contour': contour_str,
            'dilated': dilated_str,
            'blurred': blurred_str,
            'masked': masked_str,
            'restored': restored_str,
            'anomaly': anomaly_str,
        },
        'probabilities': probabilities.tolist(),
    })


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000)
