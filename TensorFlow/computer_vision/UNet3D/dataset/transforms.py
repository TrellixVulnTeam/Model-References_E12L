# Copyright (c) 2020, NVIDIA CORPORATION. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
###############################################################################
# Copyright (C) 2021 Habana Labs, Ltd. an Intel Company
###############################################################################
# Changes:
# - script migration to Tensorflow 2.x version
# - added seed setting possibility to random operations

import tensorflow as tf

from runtime.arguments import parse_args

params = parse_args()


def apply_transforms(x, y, mean, stdev, transforms):
    for _t in transforms:
        if _t is not None:
            x, y = _t(x, y, mean, stdev)
    return x, y


def apply_test_transforms(x, mean, stdev, transforms):
    for _t in transforms:
        if _t is not None:
            x = _t(x, y=None, mean=mean, stdev=stdev)
    return x


class PadXYZ:
    def __init__(self, shape=None):
        self.shape = shape

    def __call__(self, x, y, mean, stdev):
        paddings = tf.constant([[0, 0], [0, 0], [0, 5], [0, 0]])
        x = tf.pad(tensor=x, paddings=paddings, mode="CONSTANT")
        if y is None:
            return x
        y = tf.pad(tensor=y, paddings=paddings, mode="CONSTANT")
        return x, y


class CenterCrop:
    def __init__(self, shape):
        self.shape = shape

    def __call__(self, x, y, mean, stdev):
        shape = x.get_shape()
        delta = [(shape[i] - self.shape[i]) // 2 for i in range(len(self.shape))]
        x = x[
            delta[0]:delta[0] + self.shape[0],
            delta[1]:delta[1] + self.shape[1],
            delta[2]:delta[2] + self.shape[2]
        ]
        if y is None:
            return x
        y = y[
            delta[0]:delta[0] + self.shape[0],
            delta[1]:delta[1] + self.shape[1],
            delta[2]:delta[2] + self.shape[2]
        ]
        return x, y


class RandomCrop3D:
    def __init__(self, shape, margins=(0, 0, 0)):
        self.shape = shape
        self.margins = margins

    def __call__(self, x, y, mean, stdev):
        shape = x.get_shape()
        min = tf.constant(self.margins, dtype=tf.float32)
        max = tf.constant([shape[0] - self.shape[0] - self.margins[0],
                           shape[1] - self.shape[1] - self.margins[1],
                           shape[2] - self.shape[2] - self.margins[2]], dtype=tf.float32)
        center = tf.random.uniform((len(self.shape),), minval=min, maxval=max, seed=params.seed)
        center = tf.cast(center, dtype=tf.int32)
        x = x[center[0]:center[0] + self.shape[0],
              center[1]:center[1] + self.shape[1],
              center[2]:center[2] + self.shape[2]]
        if y is None:
            return x
        y = y[center[0]:center[0] + self.shape[0],
              center[1]:center[1] + self.shape[1],
              center[2]:center[2] + self.shape[2]]
        return x, y


class NormalizeImages:
    def __init__(self):
        pass

    def __call__(self, x, y, mean, stdev):
        mask = tf.math.greater(x, 0)
        x = tf.compat.v1.where(mask, (x - tf.cast(mean, x.dtype)) / (tf.cast(stdev + 1e-8, x.dtype)), x)

        if y is None:
            return x
        return x, y


class Cast:
    def __init__(self, dtype=tf.float32):
        self._dtype = dtype

    def __call__(self, x, y, mean, stdev):
        if y is None:
            return tf.cast(x, dtype=self._dtype)
        return tf.cast(x, dtype=self._dtype), y


class RandomHorizontalFlip:
    def __init__(self, threshold=0.5):
        self._threshold = threshold

    def __call__(self, x, y, mean, stdev):
        h_flip = tf.random.uniform([], seed=params.seed) > self._threshold

        x = tf.cond(pred=h_flip, true_fn=lambda: tf.reverse(x, axis=[1]), false_fn=lambda: x)
        y = tf.cond(pred=h_flip, true_fn=lambda: tf.reverse(y, axis=[1]), false_fn=lambda: y)

        return x, y


class RandomVerticalFlip:
    def __init__(self, threshold=0.5):
        self._threshold = threshold

    def __call__(self, x, y, mean, stdev):
        h_flip = tf.random.uniform([], seed=params.seed) > self._threshold

        x = tf.cond(pred=h_flip, true_fn=lambda: tf.reverse(x, axis=[0]), false_fn=lambda: x)
        y = tf.cond(pred=h_flip, true_fn=lambda: tf.reverse(y, axis=[0]), false_fn=lambda: y)

        return x, y


class RandomGammaCorrection:
    def __init__(self, gamma_range=(0.8, 1.5), keep_stats=False, threshold=0.5, epsilon=1e-8):
        self._gamma_range = gamma_range
        self._keep_stats = keep_stats
        self._eps = epsilon
        self._threshold = threshold

    def __call__(self, x, y, mean, stdev):
        augment = tf.random.uniform([], seed=params.seed) > self._threshold
        gamma = tf.random.uniform([], minval=self._gamma_range[0], maxval=self._gamma_range[1], seed=params.seed)

        x_min = tf.math.reduce_min(input_tensor=x)
        x_range = tf.math.reduce_max(input_tensor=x) - x_min

        x = tf.cond(pred=augment,
                    true_fn=lambda: tf.math.pow(((x - x_min) / float(x_range + self._eps)), gamma) * x_range + x_min,
                    false_fn=lambda: x)
        return x, y


class RandomBrightnessCorrection:
    def __init__(self, alpha=0.1, threshold=0.5, per_channel=True):
        self._alpha_range = [1.0 - alpha, 1.0 + alpha]
        self._threshold = threshold
        self._per_channel = per_channel

    def __call__(self, x, y, mean, stdev):
        mask = tf.math.greater(x, 0)
        size = x.get_shape()[-1] if self._per_channel else 1
        augment = tf.random.uniform([], seed=params.seed) > self._threshold
        correction = tf.random.uniform([size],
                                       minval=self._alpha_range[0],
                                       maxval=self._alpha_range[1],
                                       dtype=x.dtype, seed=params.seed)

        x = tf.cond(pred=augment,
                    true_fn=lambda: tf.compat.v1.where(mask, x + correction, x),
                    false_fn=lambda: x)

        return x, y


class OneHotLabels:
    def __init__(self, n_classes=1):
        self._n_classes = n_classes

    def __call__(self, x, y, mean, stdev):
        return x, tf.one_hot(y, self._n_classes)


class PadXY:
    def __init__(self, dst_size=None):
        if not dst_size:
            raise ValueError("Invalid padding size: {}".format(dst_size))

        self._dst_size = dst_size

    def __call__(self, x, y, mean, stdev):
        return tf.pad(tensor=x, paddings=self._build_padding(x)), \
            tf.pad(tensor=y, paddings=self._build_padding(y))

    def _build_padding(self, _t):
        padding = []
        for i in range(len(_t.shape)):
            if i < len(self._dst_size):
                padding.append((0, self._dst_size[i] - _t.shape[i]))
            else:
                padding.append((0, 0))
        return padding
