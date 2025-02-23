# coding=utf-8
# Copyright 2021 The Tensor2Tensor Authors.
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
# Copyright (C) 2022 Habana Labs, Ltd. an Intel Company
###############################################################################
# Changes:
# - changed tf.python.ops.alias_inplace_update to tf.add + tf.scatter_nd

"""Utilities for attention."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import collections
import functools
import itertools
import math
import operator

import numpy as np

from six.moves import range  # pylint: disable=redefined-builtin
from six.moves import zip  # pylint: disable=redefined-builtin

from TensorFlow.nlp.transformer.layers import area_attention
from TensorFlow.nlp.transformer.layers import common_layers
from TensorFlow.nlp.transformer.utils import contrib
from TensorFlow.nlp.transformer.utils import expert_utils

import tensorflow.compat.v1 as tf
import tensorflow_probability as tfp

# pylint: disable=g-direct-tensorflow-import
from tensorflow.python.framework import function
from tensorflow.python.ops import inplace_ops
# pylint: enable=g-direct-tensorflow-import


# TODO(lukaszkaiser): remove this function when not needed any more.
def layers():
  return common_layers.layers()

def to_float(x):
  """Cast x to float; created because tf.to_float is deprecated."""
  return tf.cast(x, tf.float32)

def large_compatible_negative(tensor_type):
  """Large negative number as Tensor.

  This function is necessary because the standard value for epsilon
  in this module (-1e9) cannot be represented using tf.float16

  Args:
    tensor_type: a dtype to determine the type.

  Returns:
    a large negative number.
  """
  if tensor_type == tf.float16:
    return tf.float16.min
  return -1e9


def mixed_precision_is_enabled(
    activation_dtype=None, weight_dtype=None, hparams=None):
  assert not (hparams and (activation_dtype or weight_dtype)), (
      "Provide only hparams or activation_dtype and weight_dtype")
  if (hparams and hasattr(hparams, "activation_dtype") and
      hasattr(hparams, "weight_dtype")):
    activation_dtype = hparams.activation_dtype
    weight_dtype = hparams.weight_dtype
  return activation_dtype == tf.float16 and weight_dtype == tf.float32


def maybe_upcast(logits,
                 activation_dtype=None, weight_dtype=None, hparams=None):
  if mixed_precision_is_enabled(activation_dtype, weight_dtype, hparams):
    return tf.cast(logits, tf.float32)
  return logits


# Struct containing the sequences ids and order on a batch (are send to the
# expert to allow them to compute the bias mask)
BatchInfo = collections.namedtuple("BatchInfo", "coordinates, order")

_expert_count = 0


def get_standardized_layers(hparams, dp=None):
  """Get the common attention and feed-forward layers.

  The returned layer functions will have the following signature:

    y, extra_loss = fct(x)

  extra_loss is set to 0.0 if the layer doesn't have extra loss.
  If dp is provided, the layers will be distributed within the devices.
  If moe wants to be used, both dp and model need to be set.

  Args:
    hparams (tf.HParams): the model hparameters
    dp (expert_utils.Parallelism): A data parallelism object. If not given,
      the dp calls are simply ignored.

  Returns:
    dict[str:fct]: A dictionary containing the standardized functions
  """

  def partial(fct, *args, **kwargs):
    """Same as functools.partial but with functools.wraps."""
    return functools.wraps(fct)(functools.partial(fct, *args, **kwargs))

  def register_layer(
      fct_in,
      default_args=None,
      default_kwargs=None,
      use_dp=True,
      recompute_grad=False,
  ):
    """Turn a function into its standardized version.

    Args:
      fct_in (fct): The function to register
      default_args (list): The default parameters to add to the function.
      default_kwargs (dict): The default parameters to add to the function.
        Those arguments can be overwritten when calling the function.
      use_dp (bool): Wrap the function call within a dataparallelism object if
        dp is available. Some layers (like MOE) must be called without dp.
      recompute_grad (bool): If True, recompute the function during the
        backward pass to save memory

    Returns:
      fct: the standardized layer function.
    """
    # The kwargs given when calling the function overwrite the default ones
    fct_in = partial(fct_in, *(default_args or []), **(default_kwargs or {}))

    @functools.wraps(fct_in)
    def decorator(x, *args, **kwargs):
      """Call the layer function."""
      fct = fct_in  # For closure. Could use nonlocal with Python 3
      # Eventually create the memory optimized version of the function
      if recompute_grad:
        fct = partial(fct, **kwargs)  # recompute_grad only accept args
        fct = common_layers.recompute_grad(fct)
        kwargs = {}

      # Eventually use dp (if given and not MoE)
      if use_dp and dp is not None:
        y = dp(fct, x, *args, **kwargs)
      else:
        y = fct(x, *args, **kwargs)

      # Eventually capture the extra loss
      extra_loss = 0.0
      if isinstance(y, tuple):
        y, extra_loss = y

      return y, extra_loss

    return decorator

  total_key_depth = hparams.attention_key_channels or hparams.hidden_size
  total_value_depth = hparams.attention_value_channels or hparams.hidden_size

  # Attention layers:

  # === Multi-head full attention layer ===
  multihead_attention_fn = register_layer(
      multihead_attention,
      default_kwargs=dict(
          memory_antecedent=None,  # Self-attention by default
          bias=None,
          total_key_depth=total_key_depth,
          total_value_depth=total_value_depth,
          output_depth=hparams.hidden_size,
          num_heads=hparams.num_heads,
          dropout_rate=hparams.attention_dropout,
      ))

  # === Memory efficient full-attention layer ===
  # Save memory by not storing the activations and
  # recomputing them during the backward pass
  memeff_attention_base_fn = register_layer(
      multihead_attention,
      default_kwargs=dict(
          total_key_depth=total_key_depth,
          total_value_depth=total_value_depth,
          output_depth=hparams.hidden_size,
          num_heads=hparams.num_heads,
          dropout_rate=hparams.attention_dropout,
      ),
      recompute_grad=True,
  )

  def memeff_attention_fn(*args, **kwargs):
    """Modify args/kwargs for compatibility with recompute_grad."""
    kwargs = kwargs.copy()
    assert len(args) == 1
    x = args[0]
    memory_antecedent = kwargs.pop("memory_antecedent", x)  # Same as x if None
    if kwargs.get("bias", None) is not None:  # Case where bias has been set
      args = (x, memory_antecedent, kwargs.pop("bias"))
    else:
      # Otherwise, only 2 args. This is necessary as recompute_grad does not
      # support None values.
      args = (x, memory_antecedent)
    return memeff_attention_base_fn(*args, **kwargs)

  # === Local attention (unmasked) layer ===
  # Reuse same parameters as multihead_attention
  # Don't mask the future
  local_attention_fn = partial(
      multihead_attention_fn,
      block_length=hparams.attention_loc_block_length,
      block_width=hparams.attention_loc_block_width,
      attention_type="local_unmasked",
  )

  # === Local attention (masked) layer ===
  # Reuse same parameters as multihead_attention
  # Only works for self attention. Always mask the future.
  local_attention_masked_fn = partial(
      multihead_attention_fn,
      block_length=hparams.attention_loc_block_length,
      attention_type="local_mask_right",
  )

  # === Masked memory-compressed multihead self attention layer ===
  # Only works for self attention. Always mask the future.
  compressed_attention_masked_fn = register_layer(
      multihead_self_attention_reduced,
      default_kwargs=dict(
          factor=hparams.attention_red_factor,
          nonlinearity=hparams.attention_red_nonlinearity,
          reduction_type=hparams.attention_red_type,
          multihead_params=dict(
              total_key_depth=total_key_depth,
              total_value_depth=total_value_depth,
              num_heads=hparams.num_heads,
              dropout_rate=hparams.attention_dropout,
          ),
      ),
  )

  # === Unmasked memory-compressed multihead self attention layer ===
  # Only works for self attention. Never mask the future. Bias never added
  compressed_attention_fn = partial(
      compressed_attention_masked_fn,
      add_mask=False,
  )

  # Feed-forwards layers:

  # === FC layer ===
  conv_hidden_relu = register_layer(
      common_layers.conv_hidden_relu,
      default_kwargs=dict(
          hidden_size=hparams.filter_size,
          output_size=hparams.hidden_size,
          dropout=hparams.relu_dropout,
      ),
  )

  # === Separable convolution layer ===
  # No mask applied
  sep_conv_relu = partial(
      conv_hidden_relu,
      padding="SAME",
      # Parameters copied from the transformer model, could add hparams
      kernel_size=(3, 1),
      second_kernel_size=(31, 1),
  )

  # === Separable convolution layer (masked version) ===
  # Mask the future
  sep_conv_relu_masked = partial(
      sep_conv_relu,
      padding="LEFT",  # Mask future for decoder
  )

  # Define all available layers

  cur_layers = dict(
      # Attention layers:
      a=multihead_attention_fn,  # Multihead full attention
      loc=local_attention_fn,  # Local attention
      locm=local_attention_masked_fn,  # Local attention (masked)
      red=compressed_attention_fn,  # Memory-compressed attention
      redm=compressed_attention_masked_fn,  # Memory-compressed att (masked)
      mem=memeff_attention_fn,  # Memory efficient
      # Feed-forward layers:
      fc=conv_hidden_relu,  # Fully connected
      sep=sep_conv_relu,  # Separable convolution (unmasked)
      sepm=sep_conv_relu_masked,  # Separable convolution (masked)
  )
  return cur_layers


def add_standard_attention_hparams(hparams):
  """Adds the hparams used by get_standardized_layers."""
  # All hyperparameters ending in "dropout" are automatically set to 0.0
  # when not in training mode.

  # hparams used and which should have been defined outside (in
  # common_hparams):
  # Global flags
  # hparams.mode
  # hparams.hidden_size
  # Pre-post processing flags
  # hparams.layer_preprocess_sequence
  # hparams.layer_postprocess_sequence
  # hparams.layer_prepostprocess_dropout
  # hparams.norm_type
  # hparams.norm_epsilon
  # Mixture-of-Expert flags
  # hparams.moe_hidden_sizes
  # hparams.moe_num_experts
  # hparams.moe_k
  # hparams.moe_loss_coef

  # Attention layers flags
  hparams.add_hparam("num_heads", 8)
  hparams.add_hparam("attention_key_channels", 0)
  hparams.add_hparam("attention_value_channels", 0)
  hparams.add_hparam("attention_dropout", 0.0)
  # Attention: Local
  hparams.add_hparam("attention_loc_block_length", 256)
  # Attention: Local (unmasked only): How much to look left.
  hparams.add_hparam("attention_loc_block_width", 128)
  # Attention: Memory-compressed
  hparams.add_hparam("attention_red_factor", 3)
  hparams.add_hparam("attention_red_type", "conv")
  hparams.add_hparam("attention_red_nonlinearity", "none")

  # Fully connected layers flags
  # To be more consistent, should use filter_size to also control the MOE
  # size if moe_hidden_sizes not set.
  hparams.add_hparam("filter_size", 2048)
  hparams.add_hparam("relu_dropout", 0.0)

  return hparams


def encoder_decoder_attention_loss(expected_attention_logits,
                                   actual_attentions,
                                   loss_type="kl_divergence",
                                   loss_multiplier=1.0):
  """Computes encdec attention loss between expected and actual attentions.

  Args:
    expected_attention_logits: Tensor storing the expected encoder-decoder
      attention logits with shape [batch_size, target_length, input_length].
    actual_attentions: Dictionary with actual attention logits for different
      attention types and hidden layers.
    loss_type: type of the loss function.
    loss_multiplier: multiplier for the attention loss.

  Returns:
    KL_divergence loss between the actual and expected attention logits.
  """

  def combine_attentions(attention_list):
    """Combine different layer attentions and then average over layers/heads."""
    # Stack all hidden layer attention tensors to get a tensor with shape
    # [num_hidden_layers, batch_size, num_heads, target_length, input_length].
    attentions = tf.stack(attention_list)
    # Reduce mean across all layers (axis=0) and all heads (axis=2) to get a
    # tensor with shape [batch_size, target_length, input_length].
    return tf.reduce_mean(attentions, [0, 2])

  def kl_divergence_loss(expected_logits, actual_logits):
    p = tfp.distributions.Categorical(logits=expected_logits)
    q = tfp.distributions.Categorical(logits=actual_logits)
    return tfp.distributions.kl_divergence(p, q)

  def mse_loss(expected_logits, actual_weights):
    expected_weights = tf.nn.softmax(expected_logits)
    return tf.losses.mean_squared_error(expected_weights, actual_weights)

  # For each hidden layer, we have attention-logit and attention-weight tensors
  # with shape [batch_size, num_heads, target_length, input_length].
  loss = 0.0
  if loss_type == "mse":
    actual_encdec_attention_weights = [
        t for layer_key, t in actual_attentions.items()
        if "encdec_attention" in layer_key and not layer_key.endswith("/logits")
    ]
    actual_attention_weights = combine_attentions(
        actual_encdec_attention_weights)
    loss = mse_loss(expected_attention_logits, actual_attention_weights)
  else:
    actual_encdec_attention_logits = [
        t for layer_key, t in actual_attentions.items()
        if "encdec_attention" in layer_key and layer_key.endswith("/logits")
    ]
    actual_attention_logits = combine_attentions(actual_encdec_attention_logits)
    loss = kl_divergence_loss(expected_attention_logits,
                              actual_attention_logits)
  return loss * loss_multiplier


@expert_utils.add_name_scope()
def get_timing_signal_1d(length,
                         channels,
                         min_timescale=1.0,
                         max_timescale=1.0e4,
                         start_index=0):
  """Gets a bunch of sinusoids of different frequencies.

  Each channel of the input Tensor is incremented by a sinusoid of a different
  frequency and phase.

  This allows attention to learn to use absolute and relative positions.
  Timing signals should be added to some precursors of both the query and the
  memory inputs to attention.

  The use of relative position is possible because sin(x+y) and cos(x+y) can be
  expressed in terms of y, sin(x) and cos(x).

  In particular, we use a geometric sequence of timescales starting with
  min_timescale and ending with max_timescale.  The number of different
  timescales is equal to channels / 2. For each timescale, we
  generate the two sinusoidal signals sin(timestep/timescale) and
  cos(timestep/timescale).  All of these sinusoids are concatenated in
  the channels dimension.

  Args:
    length: scalar, length of timing signal sequence.
    channels: scalar, size of timing embeddings to create. The number of
        different timescales is equal to channels / 2.
    min_timescale: a float
    max_timescale: a float
    start_index: index of first position

  Returns:
    a Tensor of timing signals [1, length, channels]
  """
  position = tf.cast(tf.range(length) + start_index, tf.float32)
  num_timescales = channels // 2
  log_timescale_increment = (
      math.log(float(max_timescale) / float(min_timescale)) /
      tf.maximum(to_float(num_timescales) - 1, 1))
  inv_timescales = min_timescale * tf.exp(
      to_float(tf.range(num_timescales)) * -log_timescale_increment)
  scaled_time = tf.expand_dims(position, 1) * tf.expand_dims(inv_timescales, 0)
  # Please note that this slightly differs from the published paper.
  # See a discussion here: https://github.com/tensorflow/tensor2tensor/pull/177
  signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=1)
  signal = tf.pad(signal, [[0, 0], [0, tf.mod(channels, 2)]])
  signal = tf.reshape(signal, [1, length, channels])
  return signal


@expert_utils.add_name_scope()
def add_timing_signal_1d(x,
                         min_timescale=1.0,
                         max_timescale=1.0e4,
                         start_index=0):
  """Adds a bunch of sinusoids of different frequencies to a Tensor.

  Each channel of the input Tensor is incremented by a sinusoid of a different
  frequency and phase.

  This allows attention to learn to use absolute and relative positions.
  Timing signals should be added to some precursors of both the query and the
  memory inputs to attention.

  The use of relative position is possible because sin(x+y) and cos(x+y) can be
  expressed in terms of y, sin(x) and cos(x).

  In particular, we use a geometric sequence of timescales starting with
  min_timescale and ending with max_timescale.  The number of different
  timescales is equal to channels / 2. For each timescale, we
  generate the two sinusoidal signals sin(timestep/timescale) and
  cos(timestep/timescale).  All of these sinusoids are concatenated in
  the channels dimension.

  Args:
    x: a Tensor with shape [batch, length, channels]
    min_timescale: a float
    max_timescale: a float
    start_index: index of first position

  Returns:
    a Tensor the same shape as x.
  """
  length = common_layers.shape_list(x)[1]
  channels = common_layers.shape_list(x)[2]
  signal = get_timing_signal_1d(length, channels, min_timescale, max_timescale,
                                start_index)
  return x + common_layers.cast_like(signal, x)


@expert_utils.add_name_scope()
def get_layer_timing_signal_learned_1d(channels, layer, num_layers):
  """get n-dimensional embedding as the layer (vertical) timing signal.

  Adds embeddings to represent the position of the layer in the tower.

  Args:
    channels: dimension of the timing signal
    layer: layer num
    num_layers: total number of layers

  Returns:
    a Tensor of timing signals [1, 1, channels].
  """
  shape = [num_layers, 1, 1, channels]
  layer_embedding = (
      tf.get_variable(
          "layer_embedding",
          shape,
          initializer=tf.random_normal_initializer(0, channels**-0.5)) *
      (channels**0.5))
  return layer_embedding[layer, :, :, :]


@expert_utils.add_name_scope()
def add_layer_timing_signal_learned_1d(x, layer, num_layers):
  """Add n-dimensional embedding as the layer (vertical) timing signal.

  Adds embeddings to represent the position of the layer in the tower.

  Args:
    x: a tensor with shape [batch, length, depth]
    layer: layer num
    num_layers: total number of layers

  Returns:
    a Tensor the same shape as x.
  """
  channels = common_layers.shape_list(x)[-1]
  signal = get_layer_timing_signal_learned_1d(channels, layer, num_layers)
  x += signal
  return x


@expert_utils.add_name_scope()
def get_layer_timing_signal_sinusoid_1d(channels, layer, num_layers):
  """Add sinusoids of different frequencies as layer (vertical) timing signal.

  Args:
    channels: dimension of the timing signal
    layer: layer num
    num_layers: total number of layers

  Returns:
    a Tensor of timing signals [1, 1, channels].
  """

  signal = get_timing_signal_1d(num_layers, channels)
  layer_signal = tf.expand_dims(signal[:, layer, :], axis=1)

  return layer_signal


@expert_utils.add_name_scope()
def add_layer_timing_signal_sinusoid_1d(x, layer, num_layers):
  """Add sinusoids of different frequencies as layer (vertical) timing signal.

  Args:
    x: a Tensor with shape [batch, length, channels]
    layer: layer num
    num_layers: total number of layers

  Returns:
    a Tensor the same shape as x.
  """

  channels = common_layers.shape_list(x)[-1]
  signal = get_layer_timing_signal_sinusoid_1d(channels, layer, num_layers)

  return x + signal


@expert_utils.add_name_scope()
def add_timing_signals_given_positions(x,
                                       positions,
                                       min_timescale=1.0,
                                       max_timescale=1.0e4):
  """Adds sinusoids of diff frequencies to a Tensor, with timing positions given.

  Args:
    x: a Tensor with shape [batch, length, channels]
    positions: a list of positions, each of which can either be a Tensor of
      shape [batch, length] or None for a default of (0..length]
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor the same shape as x.
  """
  shape = common_layers.shape_list(x)
  batch = shape[0]
  length = shape[1]
  channels = shape[2]
  num_dims = len(positions)
  num_timescales = channels // (num_dims * 2)
  log_timescale_increment = (
      math.log(float(max_timescale) / float(min_timescale)) /
      (to_float(num_timescales) - 1))
  inv_timescales = min_timescale * tf.exp(
      to_float(tf.range(num_timescales)) * -log_timescale_increment)
  for dim, position in enumerate(positions):
    if position is None:
      # Create a [batch, length] Tensor of incrementing positions 0..length-1.
      position = tf.tile(
          tf.transpose(tf.expand_dims(tf.range(0, length), axis=1)), [batch, 1])
    scaled_time = (
        tf.expand_dims(to_float(position), 2) *
        tf.expand_dims(tf.expand_dims(inv_timescales, 0), 0))
    signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=2)
    prepad = dim * 2 * num_timescales
    postpad = channels - (dim + 1) * 2 * num_timescales
    signal = tf.pad(signal, [[0, 0], [0, 0], [prepad, postpad]])
    signal = common_layers.cast_like(signal, x)
    x += signal
  return x


@expert_utils.add_name_scope()
def add_timing_signals_from_features(x,
                                     features,
                                     position_features,
                                     min_timescale=1.0,
                                     max_timescale=1.0e4):
  """Adds timing signals from features named in `position_features`.

  Args:
    x: a Tensor with shape [batch, length, channels]
    features: a features dictionary
    position_features: a comma-delimited string where each item is either a
      feature key or the empty string (which denotes a default position tensor
      of [0..length])
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor the same shape as x.
  """
  return add_timing_signals_given_positions(x, [
      features.get(position_feature)
      for position_feature in position_features.split(",")
  ], min_timescale, max_timescale)


@expert_utils.add_name_scope()
def add_timing_signal_1d_given_position(x,
                                        position,
                                        min_timescale=1.0,
                                        max_timescale=1.0e4):
  """Adds sinusoids of diff frequencies to a Tensor, with timing position given.

  Args:
    x: a Tensor with shape [batch, length, channels]
    position: a Tensor with shape [batch, length]
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor the same shape as x.
  """
  channels = common_layers.shape_list(x)[2]
  num_timescales = channels // 2
  log_timescale_increment = (
      math.log(float(max_timescale) / float(min_timescale)) /
      (tf.cast(num_timescales, tf.float32) - 1))
  inv_timescales = min_timescale * tf.exp(
      tf.cast(tf.range(num_timescales), tf.float32) * -log_timescale_increment)
  scaled_time = (
      tf.expand_dims(to_float(position), 2) * tf.expand_dims(
          tf.expand_dims(inv_timescales, 0), 0))
  signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=2)
  signal = tf.pad(signal, [[0, 0], [0, 0], [0, tf.mod(channels, 2)]])
  signal = common_layers.cast_like(signal, x)
  return x + signal


@expert_utils.add_name_scope()
def add_timing_signal_nd(x, min_timescale=1.0, max_timescale=1.0e4):
  """Adds a bunch of sinusoids of different frequencies to a Tensor.

  Each channel of the input Tensor is incremented by a sinusoid of a different
  frequency and phase in one of the positional dimensions.

  This allows attention to learn to use absolute and relative positions.
  Timing signals should be added to some precursors of both the query and the
  memory inputs to attention.

  The use of relative position is possible because sin(a+b) and cos(a+b) can be
  expressed in terms of b, sin(a) and cos(a).

  x is a Tensor with n "positional" dimensions, e.g. one dimension for a
  sequence or two dimensions for an image

  We use a geometric sequence of timescales starting with
  min_timescale and ending with max_timescale.  The number of different
  timescales is equal to channels // (n * 2). For each timescale, we
  generate the two sinusoidal signals sin(timestep/timescale) and
  cos(timestep/timescale).  All of these sinusoids are concatenated in
  the channels dimension.

  Args:
    x: a Tensor with shape [batch, d1 ... dn, channels]
    min_timescale: a float
    max_timescale: a float

  Returns:
    a Tensor the same shape as x.
  """
  num_dims = len(x.get_shape().as_list()) - 2
  channels = common_layers.shape_list(x)[-1]
  num_timescales = channels // (num_dims * 2)
  log_timescale_increment = (
      math.log(float(max_timescale) / float(min_timescale)) /
      (to_float(num_timescales) - 1))
  inv_timescales = min_timescale * tf.exp(
      to_float(tf.range(num_timescales)) * -log_timescale_increment)
  for dim in range(num_dims):
    length = common_layers.shape_list(x)[dim + 1]
    position = to_float(tf.range(length))
    scaled_time = tf.expand_dims(position, 1) * tf.expand_dims(
        inv_timescales, 0)
    signal = tf.concat([tf.sin(scaled_time), tf.cos(scaled_time)], axis=1)
    prepad = dim * 2 * num_timescales
    postpad = channels - (dim + 1) * 2 * num_timescales
    signal = tf.pad(signal, [[0, 0], [prepad, postpad]])
    for _ in range(1 + dim):
      signal = tf.expand_dims(signal, 0)
    for _ in range(num_dims - 1 - dim):
      signal = tf.expand_dims(signal, -2)
    x += signal
  return x


def add_positional_embedding(x, max_length, name=None, positions=None):
  """Adds positional embedding.

  Args:
    x: Tensor with shape [batch, length, depth].
    max_length: int representing static maximum size of any dimension.
    name: str representing name of the embedding tf.Variable.
    positions: Tensor with shape [batch, length].

  Returns:
    Tensor of same shape as x.
  """
  with tf.name_scope("add_positional_embedding"):
    _, length, depth = common_layers.shape_list(x)
    var = tf.cast(tf.get_variable(name, [max_length, depth]), x.dtype)
    if positions is None:
      pad_length = tf.maximum(0, length - max_length)
      sliced = tf.cond(
          tf.less(length, max_length),
          lambda: tf.slice(var, [0, 0], [length, -1]),
          lambda: tf.pad(var, [[0, pad_length], [0, 0]]))
      return x + tf.expand_dims(sliced, 0)
    else:
      return x + tf.gather(var, tf.to_int32(positions))


def add_positional_embedding_nd(x, max_length, name=None):
  """Adds n-dimensional positional embedding.

  The embeddings add to all positional dimensions of the tensor.

  Args:
    x: Tensor with shape [batch, p1 ... pn, depth]. It has n positional
      dimensions, i.e., 1 for text, 2 for images, 3 for video, etc.
    max_length: int representing static maximum size of any dimension.
    name: str representing name of the embedding tf.Variable.

  Returns:
    Tensor of same shape as x.
  """
  with tf.name_scope("add_positional_embedding_nd"):
    x_shape = common_layers.shape_list(x)
    num_dims = len(x_shape) - 2
    depth = x_shape[-1]
    base_shape = [1] * (num_dims + 1) + [depth]
    base_start = [0] * (num_dims + 2)
    base_size = [-1] + [1] * num_dims + [depth]
    for i in range(num_dims):
      shape = base_shape[:]
      start = base_start[:]
      size = base_size[:]
      shape[i + 1] = max_length
      size[i + 1] = x_shape[i + 1]
      var = tf.get_variable(
          name + "_%d" % i,
          shape,
          initializer=tf.random_normal_initializer(0, depth**-0.5))
      var = var * depth**0.5
      x += tf.slice(var, start, size)
    return x


def make_edge_vectors(adjacency_matrix, num_edge_types, depth, name=None):
  """Gets edge vectors for the edge types in the adjacency matrix.

  Args:
    adjacency_matrix: A [batch, num_nodes, num_nodes] tensor of ints.
    num_edge_types: Number of different edge types
    depth: Number of channels
    name: a string
  Returns:
    A [batch, num_nodes, num_nodes, depth] vector of tensors
  """
  with tf.variable_scope(name, default_name="edge_vectors"):
    att_adj_vectors_shape = [num_edge_types, depth]
    adjacency_matrix_shape = common_layers.shape_list(adjacency_matrix)
    adj_vectors = (
        tf.get_variable(
            "adj_vectors",
            att_adj_vectors_shape,
            initializer=tf.random_normal_initializer(0, depth**-0.5)) *
        (depth**0.5))
    # Avoiding gathers so that it works on TPUs
    # adjacency_matrix_one_hot has shape
    # [batch, num_nodes, num_nodes, num_edge_types]

    adjacency_matrix_one_hot = tf.one_hot(adjacency_matrix, num_edge_types)

    att_adj_vectors = tf.matmul(
        tf.reshape(to_float(adjacency_matrix_one_hot), [-1, num_edge_types]),
        adj_vectors)
    return tf.reshape(att_adj_vectors,
                      [adjacency_matrix_shape[0], adjacency_matrix_shape[1],
                       adjacency_matrix_shape[2], depth])


class LshGating(object):
  """Class to split key/queries into separate buckets."""

  def __init__(self, depth, nb_hyperplanes, nb_replicat=1, trainable=False):
    """Construct the gating function parameters.

    Compute the gates for a single head.

    Args:
      depth (int): Dimension of the key/queries to dispatch
      nb_hyperplanes (int): Nb of vectors use to split the space. Will determine
        the number of buckets (2^nb_hyperplanes - 1).
      nb_replicat (int): Redundancy to avoid the edge cases (to be in one bucket
        the input should be in a majority)
      trainable (bool): If True, a balance loss is added to force the hyperplane
        to divide the key/query space evenly
    """
    self.depth = depth
    self.nb_hyperplanes = nb_hyperplanes
    self.nb_buckets = 2**nb_hyperplanes
    self.nb_replicat = nb_replicat  # Unused for now
    self.trainable = trainable  # Unused for now

    self.dispatchers = {}

    assert self.nb_replicat == 1  # For now

    with tf.variable_scope("lsh_gating"):
      # Vectors defining the hyperplanes
      self.t_vectors = tf.get_variable(
          "vector",
          shape=(self.depth, self.nb_hyperplanes * self.nb_replicat),
          dtype=tf.float32,
          trainable=self.trainable,
      )
      # Projection vector from the bit space to similarity score space
      self.t_group = tf.constant(
          [self._idx_to_bits(i) for i in range(self.nb_buckets)],
          dtype=tf.float32,
          name="group")

  def _idx_to_bits(self, i):
    """Convert an group index to its bit representation."""
    bits = bin(i)[2:].zfill(self.nb_hyperplanes)  # Pad the bits str with 0
    return [-1.0 if b == "0" else 1.0 for b in bits]

  @expert_utils.add_name_scope("lsh_gating")
  def get_gates(self, x):
    """Return the bucket id of the given tensor.

    Args:
      x (tf.Tensor): float32 of shape [length, depth]

    Returns:
      tf.Tensor: One-hot vector int64 of shape [heads, length, nb_buckets]
        containing the id of the bucket
    """

    # The balance loss don't propagate to the rest of the network
    x = tf.stop_gradient(x)
    # [length, depth] * [depth, nb_vectors * replicat]
    x = tf.matmul(x, self.t_vectors)
    # [length, nb_vector * replicat]
    x = tf.sign(x)  # Get on which side of the hyperplane the keys are.

    # x = tf.reshape(x, [-1, nb_replicat, nb_vector])
    # [length, replicat, nb_vector] * [nb_vector, 2^nb_vector - 1]

    x = tf.matmul(x, self.t_group, transpose_b=True) / self.nb_hyperplanes
    # We get a similarity score for each of the group between [-1, 1]
    # [length, (replicat,) 2^nb_vector - 1]
    # Do an argmax to get the most likely group for each replicat
    x = tf.argmax(x, axis=-1)
    # [length(, replicat)]
    # One-hot for compatibility with the sparse dispatcher
    x = tf.one_hot(x, self.nb_buckets)
    # TODO(epot): Use a loss to force an even distribution
    return x


@expert_utils.add_name_scope()
def embedding_to_padding(emb):
  """Calculates the padding mask based on which embeddings are all zero.

  We have hacked symbol_modality to return all-zero embeddings for padding.

  Args:
    emb: a Tensor with shape [..., depth].

  Returns:
    a float Tensor with shape [...]. Each element is 1 if its corresponding
    embedding vector is all zero, and is 0 otherwise.
  """
  emb_sum = tf.reduce_sum(tf.abs(emb), axis=-1)
  return to_float(tf.equal(emb_sum, 0.0))


@expert_utils.add_name_scope()
def padding_to_length(padding):
  """Calculate the length of mask based on padding.

  Args:
    padding: a Tensor with shape [..., length].
  Returns:
    a Tensor with shape [...].
  """
  non_padding = 1.0 - padding
  return tf.to_int32(tf.reduce_sum(non_padding, axis=-1))


@expert_utils.add_name_scope()
def attention_bias_local(length, max_backward, max_forward):
  """Create an bias tensor to be added to attention logits.

  A position may attend to positions at most max_distance from it,
  forward and backwards.

  This does not actually save any computation.

  Args:
    length: int
    max_backward: int, maximum distance backward to attend. Negative values
      indicate unlimited.
    max_forward: int, maximum distance forward to attend. Negative values
      indicate unlimited.

  Returns:
    a `Tensor` with shape [1, 1, length, length].
  """
  band = common_layers.ones_matrix_band_part(
      length,
      length,
      max_backward,
      max_forward,
      out_shape=[1, 1, length, length])
  return -1e9 * (1.0 - band)


@expert_utils.add_name_scope()
def attention_bias_lower_triangle(length):
  """Create an bias tensor to be added to attention logits.

  Allows a query to attend to all positions up to and including its own.

  Args:
   length: a Scalar.

  Returns:
    a `Tensor` with shape [1, 1, length, length].
  """
  return attention_bias_local(length, -1, 0)


@expert_utils.add_name_scope()
def attention_bias_same_segment(query_segment_id, memory_segment_id):
  """Create an bias tensor to be added to attention logits.

  Positions with the same segment_ids can see each other.

  Args:
    query_segment_id: a float `Tensor` with shape [batch, query_length].
    memory_segment_id: a float `Tensor` with shape [batch, memory_length].

  Returns:
    a `Tensor` with shape [batch, 1, query_length, memory_length].
  """
  ret = (tf.cast(
      tf.not_equal(
          tf.expand_dims(query_segment_id, 2),
          tf.expand_dims(memory_segment_id, 1)), tf.float32) *
         large_compatible_negative(memory_segment_id.dtype))
  return tf.expand_dims(ret, axis=1)


@expert_utils.add_name_scope()
def attention_bias_ignore_padding(memory_padding):
  """Create an bias tensor to be added to attention logits.

  Args:
    memory_padding: a float `Tensor` with shape [batch, memory_length].

  Returns:
    a `Tensor` with shape [batch, 1, 1, memory_length].
  """
  ret = memory_padding * large_compatible_negative(memory_padding.dtype)
  return tf.expand_dims(tf.expand_dims(ret, axis=1), axis=1)


@expert_utils.add_name_scope()
def attention_bias_to_padding(attention_bias,
                              cast_fn=(lambda x: tf.cast(x, tf.float32))):
  """Inverse of attention_bias_ignore_padding().

  Args:
    attention_bias: a `Tensor` with shape [batch, 1, 1, memory_length], as
      returned by attention_bias_ignore_padding().
    cast_fn: function used to cast to output type.

  Returns:
    a Tensor with shape [batch, memory_length] with 1.0 in padding positions
    and 0.0 in non-padding positions. Type is determined by cast_fn.
  """
  # `attention_bias` is a large negative number in padding positions and 0.0
  # elsewhere.
  return tf.squeeze(cast_fn(tf.less(attention_bias, -1)), axis=[1, 2])


@expert_utils.add_name_scope()
def attention_bias_prepend_inputs_full_attention(padding):
  """Create a bias tensor for prepend_mode="prepend_inputs_full_attention".

  Produces a bias tensor to be used in self-attention.

  This bias tensor allows for full connectivity in the "inputs" part of
  the sequence and masked connectivity in the targets part.

  Args:
    padding: a float `Tensor` with shape [batch, length] with
      ones in positions corresponding to padding.  In each row, a single
      padding position separates the input part from the target part.

  Returns:
    a `Tensor` with shape [batch, 1, length, length].
  """
  # Everything past the first padding position is part of the target.
  # This Tensor has zeros for the source portion and separator,
  # and ones for the target portion.
  in_target = tf.cumsum(padding, axis=1, exclusive=True)
  # The position within the target, or 0 if part of the source.
  target_pos = tf.cumsum(in_target, axis=1)
  # A position with a lesser target_pos cannot see a position with greater
  # target_pos.
  illegal_connections = tf.greater(
      tf.expand_dims(target_pos, 1), tf.expand_dims(target_pos, 2))
  bias = to_float(illegal_connections) * -1e9
  bias = tf.expand_dims(bias, 1)
  return bias


@expert_utils.add_name_scope()
def attention_bias_proximal(length):
  """Bias for self-attention to encourage attention to close positions.

  Args:
    length: an integer scalar.

  Returns:
    a Tensor with shape [1, 1, length, length]
  """
  r = to_float(tf.range(length))
  diff = tf.expand_dims(r, 0) - tf.expand_dims(r, 1)
  return tf.expand_dims(tf.expand_dims(-tf.log1p(tf.abs(diff)), 0), 0)


@expert_utils.add_name_scope()
def attention_bias_batch(batch_coordinates_q,
                         batch_coordinates_k=None,
                         condition_fn=None):
  """Generate a mask to prevent the batch to attend to each others.

  Args:
    batch_coordinates_q: Int-like Tensor of shape [length_q, 1] containing the
      coordinates of the batches
    batch_coordinates_k: Int-like Tensor of shape [length_k, 1] containing the
      coordinates of the batches. If None, do self-attention.
    condition_fn: Callable defining the attention mask.

  Returns:
    Float-like Tensor of shape [length_q, length_k] containing either 0 or
    -infinity (-1e9).
  """
  if batch_coordinates_k is None:
    batch_coordinates_k = batch_coordinates_q

  # Convert to float first because of b/25387198.
  def to_float(bc):
    bc = tf.squeeze(bc, 1)
    bc = to_float(bc)
    return bc

  # Broadcast to create [length_q, length_k] mask.
  bc_v = tf.expand_dims(to_float(batch_coordinates_q), 1)
  bc_h = tf.expand_dims(to_float(batch_coordinates_k), 0)
  bias_batch = bc_h - bc_v
  bias_batch = condition_fn(bias_batch)
  bias_batch *= -1e9
  return bias_batch


# Mask to prevent individual sequences of the same batch to attend to each other
attention_bias_coordinates = functools.partial(
    attention_bias_batch,
    condition_fn=lambda bias: tf.minimum(1.0, tf.abs(bias)),
)

# Mask similar to upper triangular mask, but allow dispatching
attention_bias_future = functools.partial(
    attention_bias_batch,
    # Elems can attend to themselves (otherwise would use bias_batch + 1.0).
    # No tf.abs to consider the order,
    # tf.maximum and tf.minimum to threshold the values.
    condition_fn=lambda bias: tf.maximum(0.0, tf.minimum(1.0, bias)),
)


@expert_utils.add_name_scope()
def split_last_dimension(x, n):
  """Reshape x so that the last dimension becomes two dimensions.

  The first of these two dimensions is n.

  Args:
    x: a Tensor with shape [..., m]
    n: an integer.

  Returns:
    a Tensor with shape [..., n, m/n]
  """
  x_shape = common_layers.shape_list(x)
  m = x_shape[-1]
  if isinstance(m, int) and isinstance(n, int):
    assert m % n == 0
  return tf.reshape(x, x_shape[:-1] + [n, m // n])


@expert_utils.add_name_scope()
def combine_last_two_dimensions(x):
  """Reshape x so that the last two dimension become one.

  Args:
    x: a Tensor with shape [..., a, b]

  Returns:
    a Tensor with shape [..., ab]
  """
  x_shape = common_layers.shape_list(x)
  a, b = x_shape[-2:]
  return tf.reshape(x, x_shape[:-2] + [a * b])


@expert_utils.add_name_scope()
def combine_first_two_dimensions(x):
  """Reshape x so that the first two dimension become one.

  Args:
    x: a Tensor with shape [a, b, ...]

  Returns:
    a Tensor with shape [ab, ...]
  """
  ret = tf.reshape(x, tf.concat([[-1], common_layers.shape_list(x)[2:]], 0))
  old_shape = x.get_shape().dims
  a, b = old_shape[:2]
  new_shape = [a * b if a and b else None] + old_shape[2:]
  ret.set_shape(new_shape)
  return ret


@expert_utils.add_name_scope()
def split_heads(x, num_heads):
  """Split channels (dimension 2) into multiple heads (becomes dimension 1).

  Args:
    x: a Tensor with shape [batch, length, channels]
    num_heads: an integer

  Returns:
    a Tensor with shape [batch, num_heads, length, channels / num_heads]
  """
  return tf.transpose(split_last_dimension(x, num_heads), [0, 2, 1, 3])


@expert_utils.add_name_scope()
def split_heads_2d(x, num_heads):
  """Split channels (dimension 3) into multiple heads (becomes dimension 1).

  Args:
    x: a Tensor with shape [batch, height, width, channels]
    num_heads: an integer

  Returns:
    a Tensor with shape [batch, num_heads, height, width, channels / num_heads]
  """
  return tf.transpose(split_last_dimension(x, num_heads), [0, 3, 1, 2, 4])


def split_heads_nd(x, num_heads):
  """Split the depth dimension (last dimension) into multiple heads.

  Args:
    x: a [batch, d1, ..., dn, depth] tensor
    num_heads: an integer

  Returns:
    a [batch, num_heads, d1, ..., dn, depth // num_heads]
  """
  num_dimensions = len(common_layers.shape_list(x)) - 2
  return tf.transpose(
      split_last_dimension(x, num_heads), [0, num_dimensions + 1] +
      list(range(1, num_dimensions + 1)) + [num_dimensions + 2])


@expert_utils.add_name_scope()
def combine_heads(x):
  """Inverse of split_heads.

  Args:
    x: a Tensor with shape [batch, num_heads, length, channels / num_heads]

  Returns:
    a Tensor with shape [batch, length, channels]
  """
  return combine_last_two_dimensions(tf.transpose(x, [0, 2, 1, 3]))


@expert_utils.add_name_scope()
def combine_heads_2d(x):
  """Inverse of split_heads_2d.

  Args:
    x: a Tensor with shape
      [batch, num_heads, height, width, channels / num_heads]

  Returns:
    a Tensor with shape [batch, height, width, channels]
  """
  return combine_last_two_dimensions(tf.transpose(x, [0, 2, 3, 1, 4]))


def combine_heads_nd(x):
  """Inverse of split_heads_nd.

  Args:
    x: a [batch, num_heads, d1, ..., dn, depth // num_heads] tensor

  Returns:
    a [batch, d1, ...., dn, depth] tensor
  """
  num_dimensions = len(common_layers.shape_list(x)) - 3
  return combine_last_two_dimensions(
      tf.transpose(x, [0] + list(range(2, num_dimensions + 2)) +
                   [1, num_dimensions + 2]))


def attention_image_summary(attn, image_shapes=None):
  """Compute color image summary.

  Args:
    attn: a Tensor with shape [batch, num_heads, query_length, memory_length]
    image_shapes: optional tuple of integer scalars.
      If the query positions and memory positions represent the
      pixels of flattened images, then pass in their dimensions:
        (query_rows, query_cols, memory_rows, memory_cols).
      If the query positions and memory positions represent the
      pixels x channels of flattened images, then pass in their dimensions:
        (query_rows, query_cols, query_channels,
         memory_rows, memory_cols, memory_channels).
  """
  attn = tf.cast(attn, tf.float32)
  num_heads = common_layers.shape_list(attn)[1]
  # [batch, query_length, memory_length, num_heads]
  image = tf.transpose(attn, [0, 2, 3, 1])
  image = tf.pow(image, 0.2)  # for high-dynamic-range
  # Each head will correspond to one of RGB.
  # pad the heads to be a multiple of 3
  image = tf.pad(image, [[0, 0], [0, 0], [0, 0], [0, tf.mod(-num_heads, 3)]])
  image = split_last_dimension(image, 3)
  image = tf.reduce_max(image, 4)
  if image_shapes is not None:
    if len(image_shapes) == 4:
      q_rows, q_cols, m_rows, m_cols = list(image_shapes)
      image = tf.reshape(image, [-1, q_rows, q_cols, m_rows, m_cols, 3])
      image = tf.transpose(image, [0, 1, 3, 2, 4, 5])
      image = tf.reshape(image, [-1, q_rows * m_rows, q_cols * m_cols, 3])
    else:
      assert len(image_shapes) == 6
      q_rows, q_cols, q_channnels, m_rows, m_cols, m_channels = list(
          image_shapes)
      image = tf.reshape(
          image,
          [-1, q_rows, q_cols, q_channnels, m_rows, m_cols, m_channels, 3])
      image = tf.transpose(image, [0, 1, 4, 3, 2, 5, 6, 7])
      image = tf.reshape(
          image,
          [-1, q_rows * m_rows * q_channnels, q_cols * m_cols * m_channels, 3])
  tf.summary.image("attention", image, max_outputs=1)


def grouped_attention_multihead(query_antecedent,
                                memory_antecedent,
                                total_key_depth,
                                total_value_depth,
                                output_depth,
                                num_heads,
                                num_groups,
                                memory_target_density=2.0,
                                multiplicative_overhead=1.25,
                                additive_overhead=8.0,
                                mask_right=False,
                                make_image_summary=True,
                                name=None):
  """Multi-head dot-product attention with sparsity.

  For each attention head, the queries are partitioned into groups.
  For each group, only a subset of the key-value pairs are considered.

  The choices of groups are selected based on trained predictors of
  the total attention given the group inclusion.

  memory_target_density indicates the average how many groups in which
  a key-value pair should participate.

  We use auxiliary losses to ensure that each group contains roughly
  the same number of queries and the same number of key-value pairs.
  If for a given sequence, the actual number of queries/pairs sent to
  an expert exceeds this target by a factor of more than
  multiplicative_overhead, then the last ones are dropped.  We use
  this drop-last policy to avoid bleeding information backwards, which
  is necessary when using this function with autoregressive
  prediction.

  Args:
    query_antecedent: a Tensor with shape [batch, length_q, channels]
    memory_antecedent: a Tensor with shape [batch, length_m, channels]
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    num_groups: an integer
    memory_target_density: a floating point scalar
    multiplicative_overhead: a floating point scalar
    additive_overhead: a floating point scalar
    mask_right: a boolean
    make_image_summary: a boolean
    name: an optional string

  Returns:
    A Tensor with shape [batch, length_q, output_depth]

  Raises:
    ValueError: if the key depth or value depth are not divisible by the
      number of attention heads.
  """
  batch = common_layers.shape_list(query_antecedent)[0]
  length_q = common_layers.shape_list(query_antecedent)[1]
  length_kv = common_layers.shape_list(memory_antecedent)[1]

  if total_key_depth % num_heads != 0:
    raise ValueError("Key depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_key_depth, num_heads))
  depth_qk = total_key_depth // num_heads
  if total_value_depth % num_heads != 0:
    raise ValueError("Value depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_value_depth, num_heads))
  depth_v = total_value_depth // num_heads
  with tf.variable_scope(
      name, default_name="multihead_attention_sparse",
      values=[query_antecedent, memory_antecedent]):
    q = common_layers.dense(
        query_antecedent, total_key_depth, use_bias=False, name="q_transform")
    kv = common_layers.dense(
        memory_antecedent,
        total_key_depth + total_value_depth,
        use_bias=False,
        name="kv_transform")
    q = split_heads(q, num_heads)
    kv = split_heads(kv, num_heads)
    # Make predictions about q_total and m_total.
    # These are used to determine group inclusion.
    # We will train these by auxiliary losses.  We use stop_gradient here
    # to keep these losses from back-propagating to the rest of the model.
    # We add biases that help balance the usage of the experts.
    q_pred = common_layers.dense(
        tf.stop_gradient(query_antecedent),
        num_heads * num_groups,
        use_bias=False,
        name="q_pred")
    q_pred = split_heads(q_pred, num_heads)
    q_bias = tf.get_variable("q_bias", [1, num_heads, 1, num_groups])
    q_pred_biased = q_pred + q_bias
    m_pred = common_layers.dense(
        tf.stop_gradient(memory_antecedent),
        num_heads * num_groups,
        use_bias=False,
        name="m_pred")
    m_pred = split_heads(m_pred, num_heads)
    m_bias = tf.get_variable("m_bias", [1, num_heads, 1, num_groups])
    m_pred_biased = m_pred + m_bias
    q *= depth_qk**-0.5
    # q, kv, q_pred, m_pred are all [batch, heads, length_[q/m], ?]
    # now reshape them all to [batch * heads, length, ?]
    q = combine_first_two_dimensions(q)
    kv = combine_first_two_dimensions(kv)
    q_pred = combine_first_two_dimensions(q_pred)
    m_pred = combine_first_two_dimensions(m_pred)
    q_pred_biased = combine_first_two_dimensions(q_pred_biased)
    m_pred_biased = combine_first_two_dimensions(m_pred_biased)
    q_group = tf.argmax(q_pred_biased, axis=2)
    q_requests = tf.one_hot(q_group, num_groups, axis=-1)
    m_requests = to_float(tf.greater(m_pred_biased, 0.0))
    # include first memory position in all groups, to avoid division by zero.
    m_requests = tf.maximum(
        m_requests, tf.reshape(tf.one_hot([0], length_kv), [1, length_kv, 1]))
    q_group_size = tf.reduce_sum(q_requests, 1)
    m_group_size = tf.reduce_sum(m_requests, 1)
    q_group_target_size = to_float(length_q) / to_float(num_groups)
    m_group_target_size = (
        to_float(length_kv) * memory_target_density /
        to_float(num_groups))
    capacity_q = tf.minimum(
        length_q,
        tf.to_int32(q_group_target_size * multiplicative_overhead +
                    additive_overhead))
    capacity_m = tf.minimum(
        length_kv,
        tf.to_int32(m_group_target_size * multiplicative_overhead +
                    additive_overhead))
    q_dispatcher = expert_utils.TruncatingDispatcher(q_requests, capacity_q)
    m_dispatcher = expert_utils.TruncatingDispatcher(m_requests, capacity_m)
    q_gates = q_dispatcher.gates()
    m_gates = m_dispatcher.gates()
    dispatched_q = q_dispatcher.dispatch(q)
    dispatched_kv = m_dispatcher.dispatch(kv)
    # dispatched_q: [batch * num_heads, num_groups, capacity_q, depth_qk]
    # dispatched_kv:
    #   [batch * num_heads, num_groups, capacity_m, depth_qk + depth_v]
    k, v = tf.split(dispatched_kv, [depth_qk, depth_v], axis=3)
    logits = tf.matmul(dispatched_q, k, transpose_b=True)
    bias = tf.expand_dims((m_dispatcher.nonpadding() - 1.0) * 1e9, 2)
    if mask_right:
      q_coordinate = to_float(
          tf.expand_dims(q_dispatcher.length_coordinate(), 3))
      m_coordinate = to_float(
          tf.expand_dims(m_dispatcher.length_coordinate(), 2))
      bias += to_float(tf.greater(m_coordinate, q_coordinate)) * -1e9
    logits += bias
    log_weights = tf.nn.log_softmax(logits)
    weights = tf.exp(log_weights)
    # For each query, this is the log of the sum of the unnormalized weights.
    q_total = tf.stop_gradient(logits[:, :, :, :1] - log_weights[:, :, :, :1])
    # For each key, this is the sum of the normalized weights.
    m_total = tf.expand_dims(
        tf.reduce_sum(tf.stop_gradient(weights), axis=2), -1)
    o = tf.matmul(weights, v)
    o = q_dispatcher.combine(o)

    o = tf.reshape(o, [batch, num_heads, length_q, depth_v])
    o = combine_heads(o)
    o = common_layers.dense(
        o, output_depth, use_bias=False, name="output_transform")

    m_total = m_dispatcher.combine(m_total)
    q_total = q_dispatcher.combine(q_total)
    q_total = tf.squeeze(q_total, -1)
    m_total = tf.squeeze(m_total, -1)
    # Compute summed m predictions for all groups
    m_pred_used = tf.reduce_sum(tf.exp(m_pred) * m_dispatcher.gates(), axis=2)
    q_pred_used = tf.reduce_sum(q_pred * q_dispatcher.gates(), axis=2)
    epsilon = 1e-3
    m_pred_used = tf.log(m_pred_used + epsilon)
    m_total = tf.log(m_total + epsilon)
    m_loss = tf.nn.l2_loss(m_total - m_pred_used)
    q_loss = tf.nn.l2_loss(
        (q_total - q_pred_used) * tf.reduce_sum(q_gates, axis=2))

    q_loss /= to_float(batch * length_q)
    m_loss /= to_float(batch * length_kv)

    # We would like the query groups to be equal sized.  The group
    # size is discrete, so we need some trick here.  We add a loss
    # proportional to the product of the group size and the
    # predictions for that group.  This encourages the predictions to
    # decrease for groups that are too big.
    q_group_deviation = (q_group_size / q_group_target_size) - 1.0
    q_balance_loss = tf.reduce_sum(
        tf.reduce_mean(q_pred_biased, axis=1) *
        q_group_deviation) / to_float(batch)
    m_group_deviation = (m_group_size / m_group_target_size) - 1.0
    m_balance_loss = tf.reduce_sum(
        tf.reduce_mean(m_pred_biased, axis=1) *
        m_group_deviation) / to_float(batch)

    # The losses in this function only propagate back to variables
    # defined in this function, and the losses outside of this
    # function only propagate back to variables outside of this
    # function.  Assuming some kind of adaptive learning algorithm,
    # it should not matter how much we scale the losses in this function.
    # Still we scale them down a lot so that they should not show up
    # much in the overall loss for the model.
    extra_loss_multiplier = 1e-3
    extra_loss = q_loss + m_loss + q_balance_loss + m_balance_loss
    extra_loss *= extra_loss_multiplier

    # Show a bunch of summaries.
    if common_layers.should_generate_summaries() and make_image_summary:
      tf.summary.histogram("q_group_size", q_group_size)
      tf.summary.histogram("m_group_size", m_group_size)
      tf.summary.scalar("q_loss", q_loss)
      tf.summary.scalar("m_loss", m_loss)
      tf.summary.scalar("q_balance_loss", q_balance_loss)
      tf.summary.scalar("m_balance_loss", m_balance_loss)
      tf.summary.histogram("m_pred_used", m_pred_used)
      tf.summary.histogram("m_total", m_total)
      tf.summary.histogram("q_pred_used", q_pred_used)
      tf.summary.histogram("q_total", q_total)
      if make_image_summary:
        # image summaries are expensive.
        # So we restrict them to head_num<4, query_position<512, batch_index=0.
        trunc_heads = min(4, num_heads)
        trunc_length_q = tf.minimum(length_q, 512)
        # We recompute the attention for the first example, in an inefficient
        # way - masking.  This lets us show pretty pictures.
        # [trunc_heads, length_q, group]
        q_gates_trunc = q_gates[:trunc_heads, :trunc_length_q, :]
        # [trunc_heads, length_kv, group]
        m_gates_trunc = m_gates[:trunc_heads, :, :]
        grouping_mask = tf.matmul(
            q_gates_trunc, m_gates_trunc, transpose_b=True)
        q_trunc = q[:trunc_heads, :trunc_length_q, :]
        k_trunc = kv[:trunc_heads, :, :depth_qk]
        logits_trunc = tf.matmul(q_trunc, k_trunc, transpose_b=True)
        if mask_right:
          band = common_layers.ones_matrix_band_part(trunc_length_q, length_kv,
                                                     -1, 0)
          trunc_bias = tf.expand_dims((1.0 - band) * -1e9, 0)
          logits_trunc += trunc_bias
        att_trunc = tf.nn.softmax(logits_trunc)
        mask_coverage = tf.reduce_sum(grouping_mask * att_trunc) / (
            to_float(trunc_length_q) * trunc_heads)
        tf.summary.scalar("coverage", mask_coverage)
        att_trunc_hdr = tf.pow(att_trunc, 0.2)  # for high-dynamic-range
        mask_channel = grouping_mask * tf.maximum(att_trunc_hdr, 0.3)
        image = tf.stack([att_trunc_hdr, mask_channel, mask_channel], axis=3)
        tf.summary.image("att", image, max_outputs=trunc_heads)
        # show one group for each head.
        att_per_group = tf.expand_dims(weights[:trunc_heads, 0, :, :], -1)
        tf.summary.image(
            "att_per_group_%d",
            tf.pow(att_per_group, 0.2),
            max_outputs=trunc_heads)
    return o, extra_loss


def harden_attention_weights(weights, k, gumbel_noise_weight):
  """Make attention weights non-0 only on the top k ones."""
  if gumbel_noise_weight > 0.:
    gumbel_noise = -tf.log(-tf.log(tf.random_uniform(tf.shape(weights),
                                                     minval=1e-5,
                                                     maxval=1 - 1e-5)))
    weights += gumbel_noise * gumbel_noise_weight

  # Subtract the top-kth weight and zero-out all lower ones.
  # Note that currently in case of numerical ties it will retain more
  # than k elements. In the future, we may want to avoid this.
  weights -= common_layers.top_kth_iterative(weights, k)
  weights = tf.nn.relu(weights)
  # Re-normalize the weights.
  weights_sum = tf.reduce_sum(weights, axis=-1, keep_dims=True)
  weights_sum = tf.maximum(weights_sum, 1e-6)  # Avoid division by 0.
  weights /= weights_sum
  return weights


def dot_product_attention(q,
                          k,
                          v,
                          bias,
                          dropout_rate=0.0,
                          image_shapes=None,
                          name=None,
                          make_image_summary=True,
                          save_weights_to=None,
                          dropout_broadcast_dims=None,
                          activation_dtype=None,
                          weight_dtype=None,
                          hard_attention_k=0,
                          gumbel_noise_weight=0.0):
  """Dot-product attention.

  Args:
    q: Tensor with shape [..., length_q, depth_k].
    k: Tensor with shape [..., length_kv, depth_k]. Leading dimensions must
      match with q.
    v: Tensor with shape [..., length_kv, depth_v] Leading dimensions must
      match with q.
    bias: bias Tensor (see attention_bias())
    dropout_rate: a float.
    image_shapes: optional tuple of integer scalars.
      see comments for attention_image_summary()
    name: an optional string
    make_image_summary: True if you want an image summary.
    save_weights_to: an optional dictionary to capture attention weights
      for visualization; the weights tensor will be appended there under
      a string key created from the variable scope (including name).
    dropout_broadcast_dims: an optional list of integers less than rank of q.
      Specifies in which dimensions to broadcast the dropout decisions.
    activation_dtype: Used to define function activation dtype when using
      mixed precision.
    weight_dtype: The dtype weights are stored in when using mixed precision
    hard_attention_k: integer, if > 0 triggers hard attention (picking top-k)
    gumbel_noise_weight: if > 0, apply Gumbel noise with weight
      `gumbel_noise_weight` before picking top-k. This is a no op if
      hard_attention_k <= 0.

  Returns:
    Tensor with shape [..., length_q, depth_v].
  """
  with tf.variable_scope(
      name, default_name="dot_product_attention", values=[q, k, v]) as scope:
    logits = tf.matmul(q, k, transpose_b=True)  # [..., length_q, length_kv]
    if bias is not None:
      bias = common_layers.cast_like(bias, logits)
      logits += bias
    # If logits are fp16, upcast before softmax
    logits = maybe_upcast(logits, activation_dtype, weight_dtype)
    weights = tf.nn.softmax(logits, name="attention_weights")
    if hard_attention_k > 0:
      weights = harden_attention_weights(weights, hard_attention_k,
                                         gumbel_noise_weight)
    weights = common_layers.cast_like(weights, q)
    if save_weights_to is not None:
      save_weights_to[scope.name] = weights
      save_weights_to[scope.name + "/logits"] = logits
    # Drop out attention links for each head.
    weights = common_layers.dropout_with_broadcast_dims(
        weights, 1.0 - dropout_rate, broadcast_dims=dropout_broadcast_dims)
    if common_layers.should_generate_summaries() and make_image_summary:
      attention_image_summary(weights, image_shapes)
    return tf.matmul(weights, v)


def _generate_relative_positions_matrix(length_q, length_k,
                                        max_relative_position,
                                        cache=False):
  """Generates matrix of relative positions between inputs."""
  if not cache:
    if length_q == length_k:
      range_vec_q = range_vec_k = tf.range(length_q)
    else:
      range_vec_k = tf.range(length_k)
      range_vec_q = range_vec_k[-length_q:]
    distance_mat = range_vec_k[None, :] - range_vec_q[:, None]
  else:
    distance_mat = tf.expand_dims(tf.range(-length_k+1, 1, 1), 0)
  distance_mat_clipped = tf.clip_by_value(distance_mat, -max_relative_position,
                                          max_relative_position)
  # Shift values to be >= 0. Each integer still uniquely identifies a relative
  # position difference.
  final_mat = distance_mat_clipped + max_relative_position
  return final_mat


def _generate_relative_positions_embeddings(length_q, length_k, depth,
                                            max_relative_position, name,
                                            cache=False):
  """Generates tensor of size [1 if cache else length_q, length_k, depth]."""
  with tf.variable_scope(name):
    relative_positions_matrix = _generate_relative_positions_matrix(
        length_q, length_k, max_relative_position, cache=cache)
    vocab_size = max_relative_position * 2 + 1
    # Generates embedding for each relative position of dimension depth.
    embeddings_table = tf.get_variable("embeddings", [vocab_size, depth])
    embeddings = tf.gather(embeddings_table, relative_positions_matrix)
    return embeddings


def _relative_attention_inner(x, y, z, transpose):
  """Relative position-aware dot-product attention inner calculation.

  This batches matrix multiply calculations to avoid unnecessary broadcasting.

  Args:
    x: Tensor with shape [batch_size, heads, length or 1, length or depth].
    y: Tensor with shape [batch_size, heads, length or 1, depth].
    z: Tensor with shape [length or 1, length, depth].
    transpose: Whether to transpose inner matrices of y and z. Should be true if
        last dimension of x is depth, not length.

  Returns:
    A Tensor with shape [batch_size, heads, length, length or depth].
  """
  batch_size = tf.shape(x)[0]
  heads = x.get_shape().as_list()[1]
  length = tf.shape(x)[2]

  # xy_matmul is [batch_size, heads, length or 1, length or depth]
  xy_matmul = tf.matmul(x, y, transpose_b=transpose)
  # x_t is [length or 1, batch_size, heads, length or depth]
  x_t = tf.transpose(x, [2, 0, 1, 3])
  # x_t_r is [length or 1, batch_size * heads, length or depth]
  x_t_r = tf.reshape(x_t, [length, heads * batch_size, -1])
  # x_tz_matmul is [length or 1, batch_size * heads, length or depth]
  x_tz_matmul = tf.matmul(x_t_r, z, transpose_b=transpose)
  # x_tz_matmul_r is [length or 1, batch_size, heads, length or depth]
  x_tz_matmul_r = tf.reshape(x_tz_matmul, [length, batch_size, heads, -1])
  # x_tz_matmul_r_t is [batch_size, heads, length or 1, length or depth]
  x_tz_matmul_r_t = tf.transpose(x_tz_matmul_r, [1, 2, 0, 3])
  return xy_matmul + x_tz_matmul_r_t


def dot_product_attention_relative(q,
                                   k,
                                   v,
                                   bias,
                                   max_relative_position,
                                   dropout_rate=0.0,
                                   image_shapes=None,
                                   save_weights_to=None,
                                   name=None,
                                   make_image_summary=True,
                                   cache=False,
                                   allow_memory=False,
                                   hard_attention_k=0,
                                   gumbel_noise_weight=0.0):
  """Calculate relative position-aware dot-product self-attention.

  The attention calculation is augmented with learned representations for the
  relative position between each element in q and each element in k and v.

  Args:
    q: a Tensor with shape [batch, heads, length, depth].
    k: a Tensor with shape [batch, heads, length, depth].
    v: a Tensor with shape [batch, heads, length, depth].
    bias: bias Tensor.
    max_relative_position: an integer specifying the maximum distance between
        inputs that unique position embeddings should be learned for.
    dropout_rate: a floating point number.
    image_shapes: optional tuple of integer scalars.
    save_weights_to: an optional dictionary to capture attention weights
      for visualization; the weights tensor will be appended there under
      a string key created from the variable scope (including name).
    name: an optional string.
    make_image_summary: Whether to make an attention image summary.
    cache: whether use cache mode
    allow_memory: whether to assume that recurrent memory is in use. If True,
      the length dimension of k/v/bias may be longer than the queries, and it is
      assumed that the extra memory entries precede the non-memory entries.
    hard_attention_k: integer, if > 0 triggers hard attention (picking top-k)
    gumbel_noise_weight: if > 0, apply Gumbel noise with weight
      `gumbel_noise_weight` before picking top-k. This is a no op if
      hard_attention_k <= 0.

  Returns:
    A Tensor.

  Raises:
    ValueError: if max_relative_position is not > 0.
  """
  if not max_relative_position:
    raise ValueError("Max relative position (%s) should be > 0 when using "
                     "relative self attention." % (max_relative_position))
  with tf.variable_scope(
      name, default_name="dot_product_attention_relative",
      values=[q, k, v]) as scope:

    # This calculation only works for self attention.
    # q, k and v must therefore have the same shape, unless memory is enabled.
    if not cache and not allow_memory:
      q.get_shape().assert_is_compatible_with(k.get_shape())
      q.get_shape().assert_is_compatible_with(v.get_shape())

    # Use separate embeddings suitable for keys and values.
    depth = k.get_shape().as_list()[3]
    length_k = common_layers.shape_list(k)[2]
    length_q = common_layers.shape_list(q)[2] if allow_memory else length_k
    relations_keys = _generate_relative_positions_embeddings(
        length_q, length_k, depth, max_relative_position,
        "relative_positions_keys", cache=cache)
    relations_values = _generate_relative_positions_embeddings(
        length_q, length_k, depth, max_relative_position,
        "relative_positions_values", cache=cache)

    # Compute self attention considering the relative position embeddings.
    logits = _relative_attention_inner(q, k, relations_keys, True)
    if bias is not None:
      logits += bias
    weights = tf.nn.softmax(logits, name="attention_weights")
    if hard_attention_k > 0:
      weights = harden_attention_weights(weights, hard_attention_k,
                                         gumbel_noise_weight)
    if save_weights_to is not None:
      save_weights_to[scope.name] = weights
      save_weights_to[scope.name + "/logits"] = logits
    weights = tf.nn.dropout(weights, 1.0 - dropout_rate)
    if (not tf.get_variable_scope().reuse and
        common_layers.should_generate_summaries() and
        make_image_summary):
      attention_image_summary(weights, image_shapes)
    return _relative_attention_inner(weights, v, relations_values, False)


def _relative_position_to_absolute_position_masked(x):
  """Helper to dot_product_self_attention_relative_v2.

  Rearrange an attention logits or weights Tensor.

  The dimensions of the input represent:
  [batch, heads, query_position, memory_position - query_position + length - 1]

  The dimensions of the output represent:
  [batch, heads, query_position, memory_position]

  Only works with masked_attention.  Undefined behavior for regions of the
  input where memory_position > query_position.

  Args:
    x: a Tensor with shape [batch, heads, length, length]

  Returns:
    a Tensor with shape [batch, heads, length, length]
  """
  batch, heads, length, _ = common_layers.shape_list(x)
  x = tf.pad(x, [[0, 0], [0, 0], [0, 0], [1, 0]])
  x = tf.reshape(x, [batch, heads, 1 + length, length])
  x = tf.slice(x, [0, 0, 1, 0], [-1, -1, -1, -1])
  return x


def _absolute_position_to_relative_position_masked(x):
  """Helper to dot_product_self_attention_relative_v2.

  Rearrange an attention logits or weights Tensor.

  The dimensions of the input represent:
  [batch, heads, query_position, memory_position]

  The dimensions of the output represent:
  [batch, heads, query_position, memory_position - query_position + length - 1]

  Only works with masked_attention.  Undefined behavior for regions of the
  input where memory_position > query_position.

  Args:
    x: a Tensor with shape [batch, heads, length, length]

  Returns:
    a Tensor with shape [batch, heads, length, length]
  """
  batch, heads, length, _ = common_layers.shape_list(x)
  x = tf.pad(x, [[0, 0], [0, 0], [1, 0], [0, 0]])
  x = tf.reshape(x, [batch, heads, length, length + 1])
  x = tf.slice(x, [0, 0, 0, 1], [batch, heads, length, length])
  return x


def get_relative_embeddings_left(max_relative_position, length, depth,
                                 num_heads, heads_share_relative_embedding,
                                 name):
  """Instantiate or retrieve relative embeddings, sliced according to length.

  Use for masked case where the relative attention is only looking left.

  Args:
    max_relative_position: an Integer for the number of entries in the relative
      embedding, which corresponds to the max relative distance that is
      considered.
    length: an Integer, specifies the length of the input sequence for which
      this relative embedding is retrieved for.
    depth: an Integer, specifies the depth for relative embeddings.
    num_heads: an Integer, specifies the number of heads.
    heads_share_relative_embedding: a Boolean specifying if the relative
      embedding is shared across heads.
    name: a string giving the name of the embedding variables.

  Returns:
    a Tensor with shape [length, depth]
  """
  initializer_stddev = depth**-0.5
  if heads_share_relative_embedding:
    embedding_shape = (max_relative_position, depth)
  else:
    embedding_shape = (num_heads, max_relative_position, depth)
  relative_embeddings = tf.get_variable(
      name=name, shape=embedding_shape,
      initializer=tf.random_normal_initializer(stddev=initializer_stddev))
  # Pad first before slice to avoid using tf.cond.
  pad_length = tf.maximum(length - max_relative_position, 0)
  start_slice_position = tf.maximum(max_relative_position - length, 0)
  if heads_share_relative_embedding:
    padded_relative_embeddings = tf.pad(
        relative_embeddings,
        [[pad_length, 0], [0, 0]])
    used_relative_embeddings = tf.slice(
        padded_relative_embeddings,
        [start_slice_position, 0], [length, -1])
  else:
    padded_relative_embeddings = tf.pad(
        relative_embeddings,
        [[0, 0], [pad_length, 0], [0, 0]])
    used_relative_embeddings = tf.slice(
        padded_relative_embeddings,
        [0, start_slice_position, 0], [-1, length, -1])
  return used_relative_embeddings


def dot_product_self_attention_relative_v2(q,
                                           k,
                                           v,
                                           bias,
                                           max_relative_position=None,
                                           dropout_rate=0.0,
                                           image_shapes=None,
                                           save_weights_to=None,
                                           name=None,
                                           make_image_summary=True,
                                           dropout_broadcast_dims=None,
                                           heads_share_relative_embedding=False,
                                           add_relative_to_values=False):
  """Calculate relative position-aware dot-product self-attention.

  Only works for masked self-attention (no looking forward).

  The attention calculation is augmented with learned representations for the
  relative position between each element in q and each element in k and v.

  Args:
    q: a Tensor with shape [batch, heads, length, depth].
    k: a Tensor with shape [batch, heads, length, depth].
    v: a Tensor with shape [batch, heads, length, depth].
    bias: bias Tensor.
    max_relative_position: an integer indicating the maximum relative distance
      to look back - changing this invalidates checkpoints
    dropout_rate: a floating point number.
    image_shapes: optional tuple of integer scalars.
    save_weights_to: an optional dictionary to capture attention weights
      for visualization; the weights tensor will be appended there under
      a string key created from the variable scope (including name).
    name: an optional string.
    make_image_summary: Whether to make an attention image summary.
    dropout_broadcast_dims:  an optional list of integers less than 4
      specifying in which dimensions to broadcast the dropout decisions.
      saves memory.
    heads_share_relative_embedding: a boolean indicating wheather to share
      relative embeddings between attention heads.
    add_relative_to_values: a boolean for whether to add relative component to
      values.

  Returns:
    A Tensor.

  Raises:
    ValueError: if max_relative_position is not > 0.
  """
  if not max_relative_position:
    raise ValueError("Max relative position (%s) should be > 0 when using "
                     "relative self attention." % (max_relative_position))
  with tf.variable_scope(
      name,
      default_name="dot_product_self_attention_relative_v2",
      values=[q, k, v]) as scope:

    # This calculation only works for self attention.
    # q, k and v must therefore have the same shape.
    # (Except v can have different depth.)
    q.get_shape().assert_is_compatible_with(k.get_shape())
    q.get_shape()[:-1].assert_is_compatible_with(v.get_shape()[:-1])

    # Use separate embeddings suitable for keys and values.
    _, num_heads, length, depth_k = common_layers.shape_list(k)

    # [batch, num_heads, query_length, memory_length]
    logits = tf.matmul(q, k, transpose_b=True)
    key_relative_embeddings = get_relative_embeddings_left(
        max_relative_position, length, depth_k, num_heads,
        heads_share_relative_embedding, "key_relative_embeddings")

    rel_logits = matmul_with_relative_keys(q, key_relative_embeddings,
                                           heads_share_relative_embedding)
    rel_logits = _relative_position_to_absolute_position_masked(rel_logits)
    logits += rel_logits
    if bias is not None:
      logits += bias

    weights = tf.nn.softmax(logits, name="attention_weights")
    if save_weights_to is not None:
      save_weights_to[scope.name] = weights
      save_weights_to[scope.name + "/logits"] = logits
    # Dropping out the attention links for each of the heads.
    weights = common_layers.dropout_with_broadcast_dims(
        weights, 1.0 - dropout_rate, broadcast_dims=dropout_broadcast_dims)
    if common_layers.should_generate_summaries() and make_image_summary:
      attention_image_summary(weights, image_shapes)
    output = tf.matmul(weights, v)
    if add_relative_to_values:
      # [batch, num_heads, query_length, memory_length]
      relative_weights = _absolute_position_to_relative_position_masked(weights)
      depth_v = common_layers.shape_list(v)[3]
      value_relative_embeddings = get_relative_embeddings_left(
          max_relative_position, length, depth_v, num_heads,
          heads_share_relative_embedding, "value_relative_embeddings")
      output += matmul_with_relative_values(
          relative_weights, value_relative_embeddings,
          heads_share_relative_embedding)
    return output


def _absolute_position_to_relative_position_unmasked(x):
  """Helper function for dot_product_unmasked_self_attention_relative_v2.

  Rearrange an attention logits or weights Tensor.

  The dimensions of the input represent:
  [batch, heads, query_position, memory_position]

  The dimensions of the output represent:
  [batch, heads, query_position, memory_position - query_position + length - 1]

  Only works with unmasked_attention.

  Args:
    x: a Tensor with shape [batch, heads, length, length]

  Returns:
    a Tensor with shape [batch, heads, length, 2*length-1]
  """
  batch, heads, length, _ = common_layers.shape_list(x)
  # padd along column
  x = tf.pad(x, [[0, 0], [0, 0], [0, 0], [0, length-1]])
  x_flat = tf.reshape(x, [batch, heads, length**2 + length*(length -1)])
  # add 0's in the beginning that will skew the elements after reshape
  x_flat = tf.pad(x_flat, [[0, 0], [0, 0], [length, 0]])
  x = tf.reshape(x_flat, [batch, heads, length, 2*length])
  x = tf.slice(x, [0, 0, 0, 1], [batch, heads, length,
                                 2*length -1])
  return x


def get_relative_embeddings_left_right(max_relative_position, length, depth,
                                       num_heads,
                                       heads_share_relative_embedding,
                                       name):
  """Instantiate or retrieve relative embeddings, sliced according to length.

  Use for unmasked case where the relative attention looks both left and right.

  Args:
    max_relative_position: an Integer for the number of entries in the relative
      embedding, which corresponds to the max relative distance that is
      considered.
    length: an Integer, specifies the length of the input sequence for which
      this relative embedding is retrieved for.
    depth: an Integer, specifies the depth for relative embeddings.
    num_heads: an Integer, specifies the number of heads.
    heads_share_relative_embedding: a Boolean specifying if the relative
      embedding is shared across heads.
    name: a string giving the name of the embedding variables.

  Returns:
    a Tensor with shape [length, depth]
  """
  initializer_stddev = depth**-0.5
  max_relative_position_unmasked = 2 * max_relative_position - 1
  if heads_share_relative_embedding:
    embedding_shape = (max_relative_position_unmasked, depth)
  else:
    embedding_shape = (num_heads, max_relative_position_unmasked, depth)
  relative_embeddings = tf.get_variable(
      name=name, shape=embedding_shape,
      initializer=tf.random_normal_initializer(stddev=initializer_stddev))
  # Pad first before slice to avoid using tf.cond.
  pad_length = tf.maximum(length - max_relative_position, 0)
  slice_start_position = tf.maximum(max_relative_position-length, 0)
  if heads_share_relative_embedding:
    padded_relative_embeddings = tf.pad(
        relative_embeddings,
        [[pad_length, pad_length], [0, 0]])
    used_relative_embeddings = tf.slice(
        padded_relative_embeddings,
        [slice_start_position, 0], [2 * length - 1, -1])
  else:
    padded_relative_embeddings = tf.pad(
        relative_embeddings,
        [[0, 0], [pad_length, pad_length], [0, 0]])
    used_relative_embeddings = tf.slice(
        padded_relative_embeddings,
        [0, slice_start_position, 0], [-1, 2 * length - 1, -1])
  return used_relative_embeddings


def dot_product_unmasked_self_attention_relative_v2(
    q, k, v, bias, max_relative_position=None, dropout_rate=0.0,
    image_shapes=None, save_weights_to=None, name=None, make_image_summary=True,
    dropout_broadcast_dims=None, heads_share_relative_embedding=False,
    add_relative_to_values=False):
  """Calculate relative position-aware dot-product self-attention.

  The attention calculation is augmented with learned representations for the
  relative position between each element in q and each element in k and v.

  Args:
    q: a Tensor with shape [batch, heads, length, depth].
    k: a Tensor with shape [batch, heads, length, depth].
    v: a Tensor with shape [batch, heads, length, depth].
    bias: bias Tensor.
    max_relative_position: an integer the max relative embedding considered.
      Changing this invalidates checkpoints.
    dropout_rate: a floating point number.
    image_shapes: optional tuple of integer scalars.
    save_weights_to: an optional dictionary to capture attention weights
      for visualization; the weights tensor will be appended there under
      a string key created from the variable scope (including name).
    name: an optional string.
    make_image_summary: Whether to make an attention image summary.
    dropout_broadcast_dims:  an optional list of integers less than 4
      specifying in which dimensions to broadcast the dropout decisions.
      saves memory.
    heads_share_relative_embedding: a boolean indicating wheather to share
      relative embeddings between attention heads.
    add_relative_to_values: a boolean for whether to add relative component to
      values.

  Returns:
    A Tensor.

  Raises:
    ValueError: if max_relative_position is not > 0.
  """
  if not max_relative_position:
    raise ValueError("Max relative position (%s) should be > 0 when using "
                     "relative self attention." % (max_relative_position))

  with tf.variable_scope(
      name,
      default_name="dot_product_unmasked_self_attention_relative_v2",
      values=[q, k, v]) as scope:

    # This calculation only works for self attention.
    # q, k and v must therefore have the same shape.
    q.get_shape().assert_is_compatible_with(k.get_shape())
    q.get_shape().assert_is_compatible_with(v.get_shape())

    # [batch, num_heads, query_length, memory_length]
    logits = tf.matmul(q, k, transpose_b=True)

    length = common_layers.shape_list(q)[2]
    k_shape = common_layers.shape_list(k)
    num_heads = k_shape[1]
    depth_k = k_shape[-1]

    key_relative_embeddings = get_relative_embeddings_left_right(
        max_relative_position, length, depth_k, num_heads,
        heads_share_relative_embedding,
        "key_relative_embeddings")
    unmasked_rel_logits = matmul_with_relative_keys(
        q, key_relative_embeddings, heads_share_relative_embedding)
    unmasked_rel_logits = _relative_position_to_absolute_position_unmasked(
        unmasked_rel_logits)
    logits += unmasked_rel_logits

    if bias is not None:
      logits += bias
    weights = tf.nn.softmax(logits, name="attention_weights")
    if save_weights_to is not None:
      save_weights_to[scope.name] = weights
      save_weights_to[scope.name + "/logits"] = logits
    # dropping out the attention links for each of the heads
    weights = common_layers.dropout_with_broadcast_dims(
        weights, 1.0 - dropout_rate, broadcast_dims=dropout_broadcast_dims)
    # relative_weights.set_shape([None, None, None, max_length])
    if common_layers.should_generate_summaries() and make_image_summary:
      attention_image_summary(weights, image_shapes)
    ret = tf.matmul(weights, v)
    if add_relative_to_values:
      # Adds the contribution of the weighted relative embeddings to the values.
      # [batch, num_heads, query_length, 2*memory_length-1]
      relative_weights = _absolute_position_to_relative_position_unmasked(
          weights)
      depth_v = common_layers.shape_list(v)[3]
      value_relative_embeddings = get_relative_embeddings_left_right(
          max_relative_position, length, depth_v, num_heads,
          heads_share_relative_embedding, "value_relative_embeddings")
      ret += matmul_with_relative_values(
          relative_weights, value_relative_embeddings,
          heads_share_relative_embedding)
    return ret


def _matmul_with_relative_keys_2d(x, y, heads_share_relative_embedding):
  """Helper function for dot_product_unmasked_self_attention_relative_2d."""
  if heads_share_relative_embedding:
    ret = tf.einsum("bhxyd,md->bhxym", x, y)
  else:
    ret = tf.einsum("bhxyd,hmd->bhxym", x, y)
  return ret


def dot_product_unmasked_self_attention_relative_2d(
    q, k, v, bias, max_relative_position=None, dropout_rate=0.0,
    image_shapes=None, name=None, make_image_summary=True,
    dropout_broadcast_dims=None, heads_share_relative_embedding=False,
    add_relative_to_values=False):
  """Calculate relative position unmasked dot-product self-attention 2d.


  The attention calculation is augmented with learned representations for the
  relative position between each element in q and each element in k and v in
  height and width dimensions. for query index (i,j) and key index (l, m),
  the logit is q_i k_j^T + q_i rh_{l-i}^T + q_i rw_{m-j}^T, where rh and ry are
  the set of relative embeddings in height and width spatial dimensions,
  respectively.

  Args:
    q: a Tensor with shape [batch, heads, height, width, depth].
    k: a Tensor with shape [batch, heads, height, width, depth].
    v: a Tensor with shape [batch, heads, height, width, depth].
    bias: bias Tensor.
    max_relative_position: an integer the max relative embedding considered.
      Changing this invalidates checkpoints.
    dropout_rate: a floating point number.
    image_shapes: optional tuple of integer scalars.
    name: an optional string.
    make_image_summary: Whether to make an attention image summary.
    dropout_broadcast_dims:  an optional list of integers less than 4
      specifying in which dimensions to broadcast the dropout decisions.
      saves memory.
    heads_share_relative_embedding: a boolean indicating wheather to share
      relative embeddings between attention heads.
    add_relative_to_values: a boolean for adding relative embeddings to values.

  Returns:
    [batch, heads, height, width, depth] tensor, the output of attention.
    height_key_relative_embeddings: a 3d or 2d tensor, depending on head sharing
      settings, which are the relative embeddings for height.
    width_key_relative_embeddings: a 3d or 2d tensor, depending on head sharing
      settings, which are the relative embeddings for width.

  Raises:
    ValueError: if max_relative_position is not > 0.
  """
  if not max_relative_position:
    raise ValueError("Max relative position (%s) should be > 0 when using "
                     "relative self attention." % (max_relative_position))

  if add_relative_to_values:
    raise ValueError("Adding relative embeddings to values is not implemented")

  with tf.variable_scope(
      name,
      default_name="dot_product_self_attention_relative_v2",
      values=[q, k, v]):

    # This calculation only works for self attention.
    # q, k and v must therefore have the same shape.
    q.get_shape().assert_is_compatible_with(k.get_shape())
    q.get_shape()[:-1].assert_is_compatible_with(v.get_shape()[:-1])

    (height, width) = (common_layers.shape_list(q)[2],
                       common_layers.shape_list(q)[3])
    k_shape = common_layers.shape_list(k)
    num_heads = k_shape[1]
    depth_k = k_shape[-1]
    depth_v = common_layers.shape_list(v)[-1]
    # flatten height width
    flatten_hw = lambda x, d: tf.reshape(x, [-1, num_heads, height*width, d])
    # [batch, num_heads, query_length, memory_length]
    logits = tf.matmul(flatten_hw(q, depth_k), flatten_hw(k, depth_k),
                       transpose_b=True)

    def _compute_2d_relative_logits(
        query, key_relative_embeddings, height, width,
        heads_share_relative_embedding, transpose_mask):
      """compute relative logits."""
      unmasked_rel_logits = _matmul_with_relative_keys_2d(
          query, key_relative_embeddings, heads_share_relative_embedding)
      # collapse height and heads
      unmasked_rel_logits = tf.reshape(unmasked_rel_logits,
                                       [-1, num_heads*height, width,
                                        2*width-1])
      unmasked_rel_logits = (
          _relative_position_to_absolute_position_unmasked(
              unmasked_rel_logits))
      # shape it back for tiling
      unmasked_rel_logits = tf.reshape(
          unmasked_rel_logits, [-1, num_heads, height, width, width])
      # tiling it height times
      unmasked_rel_logits = tf.expand_dims(
          unmasked_rel_logits, axis=3)
      unmasked_rel_logits = tf.tile(unmasked_rel_logits,
                                    [1, 1, 1, height, 1, 1])
      # bringing it to the right shape for adding to the logits.
      unmasked_rel_logits = tf.transpose(unmasked_rel_logits, transpose_mask)
      unmasked_rel_logits = tf.reshape(unmasked_rel_logits,
                                       [-1, num_heads, height*width,
                                        height*width])
      return unmasked_rel_logits

    # Relative logits in width dimension first.
    width_key_relative_embeddings = get_relative_embeddings_left_right(
        max_relative_position, width, depth_k, num_heads,
        heads_share_relative_embedding,
        "width_key_relative_embeddings")
    # [batch, heads, height, 2*width-1, 2*width-1]
    width_unmasked_rel_logits = _compute_2d_relative_logits(
        q, width_key_relative_embeddings, height, width,
        heads_share_relative_embedding, [0, 1, 2, 4, 3, 5])
    logits += width_unmasked_rel_logits
    # Relative logits in height dimension next. For ease, we transpose
    # height and width and repeat the above steps, and transpose to eventually
    # put the logits in their right positions.
    # [batch, heads, height, 2*height-1, 2*width-1]
    height_key_relative_embeddings = get_relative_embeddings_left_right(
        max_relative_position, height, depth_k, num_heads,
        heads_share_relative_embedding,
        "height_key_relative_embeddings")

    height_unmasked_rel_logits = _compute_2d_relative_logits(
        tf.transpose(q, [0, 1, 3, 2, 4]),
        height_key_relative_embeddings,
        width,
        height,
        heads_share_relative_embedding, [0, 1, 4, 2, 5, 3])
    logits += height_unmasked_rel_logits
    if bias is not None:
      logits += bias
    weights = tf.nn.softmax(logits, name="attention_weights")
    # dropping out the attention links for each of the heads
    weights = common_layers.dropout_with_broadcast_dims(
        weights, 1.0 - dropout_rate, broadcast_dims=dropout_broadcast_dims)
    if common_layers.should_generate_summaries() and make_image_summary:
      attention_image_summary(weights, image_shapes)
    ret = tf.matmul(weights, flatten_hw(v, depth_v))
    # reshape back the same spatial dimensions as q
    return (
        tf.reshape(ret, [-1, num_heads, height, width, depth_v]),
        height_key_relative_embeddings,
        width_key_relative_embeddings)


def _split_along_width(x_left_right_blocks):
  """Helper function for local 2d attention.

  Takes a tensor of [batch, heads, num_h_blocks, num_w_blocks,
  height, width, depth] and returns two tensors which contain every alternate
  position along the width


  Args:
    x_left_right_blocks: A [batch, num_h_blocks, num_w_blocks,
                            height, width, depth] tensor

  Returns:
    x_left_blocks, x_right_blocks: two [batch, num_h_blocks,
                                        (num_w_blocks-2)/2, height, width,
                                        depth] tensors

  """
  (_, x_num_h_blocks, x_num_outer_w_blocks, x_memory_flange_h,
   x_memory_flange_w, depth) = common_layers.shape_list(x_left_right_blocks)
  x_num_w_blocks = (x_num_outer_w_blocks-1)//2
  # get it ready for splitting the left and right memory blocks
  x_left_right_blocks = tf.reshape(x_left_right_blocks,
                                   [-1,
                                    x_num_h_blocks,
                                    x_num_outer_w_blocks//2, 2,
                                    x_memory_flange_h,
                                    x_memory_flange_w, depth])

  x_left_blocks, x_right_blocks = tf.split(x_left_right_blocks,
                                           num_or_size_splits=2, axis=3)
  x_left_blocks = tf.squeeze(x_left_blocks, axis=3)
  x_right_blocks = tf.squeeze(x_right_blocks, axis=3)
  x_left_blocks = tf.slice(x_left_blocks, [0, 0, 0, 0, 0, 0],
                           [-1, -1, x_num_w_blocks, -1, -1, -1])
  x_right_blocks = tf.slice(x_right_blocks, [0, 0, 1, 0, 0, 0],
                            [-1, -1, x_num_w_blocks, -1, -1, -1])
  return x_left_blocks, x_right_blocks


def _get_left_right_blocks(x):
  """Helper function. Assumes that memory_flange is half of query sizes.

  This function splits the tensor of width 'n' into two halves, where the
  first half gets the width indices 0, 2, 4.. and the second half gets the
  width indices 3, 5, ... We also fuse two blocks along the h dimension.

  Args:
    x: a 6-d tensor.

  Returns:
    x_left_blocks, x_right_blocks: Two 6-d tensors
  """
  (_, x_num_outer_h_blocks, x_num_outer_w_blocks, x_memory_flange_h,
   x_memory_flange_w, depth) = common_layers.shape_list(x)
  x_left_right_blocks = tf.slice(x,
                                 [0, 1, 0, 0, 0, 0],
                                 [-1, x_num_outer_h_blocks-2, -1, -1,
                                  -1, -1])
  num_blocks_h = (x_num_outer_h_blocks-2)//2
  x_left_right_blocks = tf.reshape(x_left_right_blocks,
                                   [-1,
                                    num_blocks_h,
                                    2, x_num_outer_w_blocks,
                                    x_memory_flange_h,
                                    x_memory_flange_w, depth])
  x_left_right_blocks = tf.transpose(x_left_right_blocks,
                                     [0, 1, 3, 2, 4, 5, 6])
  x_left_right_blocks = tf.reshape(x_left_right_blocks,
                                   [-1, num_blocks_h,
                                    x_num_outer_w_blocks, 2*x_memory_flange_h,
                                    x_memory_flange_w, depth])
  # get it ready for splitting the left and right memory blocks
  x_left_blocks, x_right_blocks = _split_along_width(x_left_right_blocks)

  return x_left_blocks, x_right_blocks
  # return x_left_right_blocks


def _extract_blocks(x, block_h, block_w):
  """Helper function for local 2d attention.

  Args:
    x: a [batch, height, width, depth] tensor
    block_h: An integer. block height
    block_w: An inteter. block width

  Returns:
    a [batch, num_heads, height/block_h, width/block_w, depth] tensor
  """
  (_, height, width, depth) = common_layers.shape_list(x)
  assert height % block_h == 0
  assert width % block_w == 0
  x = tf.reshape(x, [-1, height//block_h, block_h,
                     width//block_w, block_w, depth])
  return tf.transpose(x, [0, 1, 3, 2, 4, 5])


def get_2d_local_memory(x, query_shape, memory_flange):
  """Stitches together the local 2d memory blocks.

  Args:
    x: a [batch, height, width, depth tensor]
    query_shape: 2-d integer list of query shape
    memory_flange: 2-d integer list of memory flanges

  Returns:
    x: A [batch, num_h_blocks, num_w_blocks,
          query_shape[0]+2*memory_flange[0],query_shape[1]+2*memory_flange[1]]
          tensor.
  """
  (_, height, width, depth_x) = common_layers.shape_list(x)
  x_center_blocks = _extract_blocks(x, query_shape[0], query_shape[1])
  # add extra padding to x so that we can extract the memory region
  # around the center
  paddings = [[0, 0], [memory_flange[0], memory_flange[0]],
              [memory_flange[1], memory_flange[1]], [0, 0]]
  padded_x = tf.pad(x, paddings)
  padded_x.set_shape([None, height+2*memory_flange[0],
                      width+2*memory_flange[1], depth_x])
  x_outer_memory_blocks = _extract_blocks(padded_x,
                                          memory_flange[0], memory_flange[1])
  # We'll extract left and right memory blocks, top and bottom memory blocks,
  # and then the corner memory blocks

  # Each of these after  will have shape
  # [batch, num_h_blocks, num_w_blocks, query_shape[0],
  # memory_flange[1], depth]
  x_left_blocks, x_right_blocks = _get_left_right_blocks(
      x_outer_memory_blocks)
  t_hw_block = lambda x: tf.transpose(x, [0, 2, 1, 4, 3, 5])
  # now to get top and bottom blocks, we should just transpose the outer
  # blocks, call the same function and transpose back to get shape
  # [batch, num_h_blocks, num_w_blocks, memory_flange[0],
  # query_shape[1], depth]
  x_top_center_blocks, x_bottom_center_blocks = (
      map(t_hw_block, _get_left_right_blocks(
          t_hw_block(x_outer_memory_blocks))))

  # now to get the corner blocks
  x_left_corner_blocks, x_right_corner_blocks = _split_along_width(
      x_outer_memory_blocks)
  # now to extract top and bottom for both k and v
  # we need to transpose because _split_along_width separates along
  # the width
  # each of these should have shape [batch, num_h_blocks,
  # num_w_blocks, memory_flange[0], memory_flange[1], depth]

  t_hw = lambda x: tf.transpose(x, [0, 2, 1, 3, 4, 5])
  x_top_left_corner_blocks, x_bottom_left_corner_blocks = (
      map(t_hw, _split_along_width(t_hw(x_left_corner_blocks))))
  x_top_right_corner_blocks, x_bottom_right_corner_blocks = (
      map(t_hw, _split_along_width(t_hw(x_right_corner_blocks))))

  # The memory is top_left     top_center    top_right
  #               left_center  middle        right_center
  #               bottom_left  bottom_center bottom_right
  # Assembling the above row by row
  # first [x_top_left, x_top, x_top_right]
  # to get [batch, num_h_blocks, num_w_blocks, memory_flange[0],
  # query_shape[1]+2*memory_flange[1], depth]
  # then [x_left, x_center, x_right]
  # then [x_bottom_left, x_bottom, x_bottom_right]
  x_top_memory = tf.concat(
      [x_top_left_corner_blocks,
       x_top_center_blocks,
       x_top_right_corner_blocks], axis=4)
  x_middle_memory = tf.concat(
      [x_left_blocks, x_center_blocks, x_right_blocks], axis=4)
  x_bottom_memory = tf.concat(
      [x_bottom_left_corner_blocks,
       x_bottom_center_blocks,
       x_bottom_right_corner_blocks], axis=4)

  # concat along height
  x = tf.concat([x_top_memory, x_middle_memory, x_bottom_memory], axis=3)
  return x


def get_2d_local_memory_v2(x, query_shape, memory_flange):
  """Gathering memory blocks around query blocks. flange is half of query .

    Only works if memory flanges are half of query sizes.

  Args:
    x: a [batch, height, width, depth tensor]
    query_shape: 2-d integer list of query shape
    memory_flange: 2-d integer list of memory flanges

  Returns:
    x: A [batch, num_h_blocks, num_w_blocks,
          query_shape[0]+2*memory_flange[0],query_shape[1]+2*memory_flange[1]]
          tensor.
  """
  (_, height, width, depth_x) = common_layers.shape_list(x)
  # add extra padding to x so that we can extract the memory region
  # around the center
  paddings = [[0, 0], [memory_flange[0], memory_flange[0]],
              [memory_flange[1], memory_flange[1]], [0, 0]]
  padded_x = tf.pad(x, paddings)
  padded_x.set_shape([None, height+2*memory_flange[0],
                      width+2*memory_flange[1], depth_x])
  num_h_memory_blocks = height//query_shape[0] + 1
  num_w_memory_blocks = width//query_shape[1] + 1
  x_memory_blocks = _extract_blocks(padded_x,
                                    query_shape[0], query_shape[1])
  x_width_blocks = tf.split(x_memory_blocks, num_w_memory_blocks,
                            2)
  x_left_width = tf.concat(x_width_blocks[:num_w_memory_blocks - 1], axis=2)
  x_right_width = tf.concat(x_width_blocks[1:], axis=2)
  x_memory_blocks = tf.concat([x_left_width, x_right_width], axis=4)

  x_height_blocks = tf.split(x_memory_blocks, num_h_memory_blocks, 1)
  x_top_height = tf.concat(x_height_blocks[:num_h_memory_blocks - 1], axis=1)
  x_bottom_height = tf.concat(x_height_blocks[1:], axis=1)
  x = tf.concat([x_top_height, x_bottom_height], axis=3)

  return x


def dot_product_unmasked_attention_local_2d_tpu(
    q, k, v, bias, max_relative_position=None, query_shape=(8, 8),
    dropout_rate=0.0, image_shapes=None, name=None, make_image_summary=False,
    dropout_broadcast_dims=None):
  """Calculate unmasked dot-product local self-attention 2d on tpu.

  Args:
    q: a Tensor with shape [batch, heads, height, width, depth].
    k: a Tensor with shape [batch, heads, height, width, depth].
    v: a Tensor with shape [batch, heads, height, width, depth].
    bias: bias Tensor.
    max_relative_position: an integer the max relative embedding considered.
      Changing this invalidates checkpoints.
    query_shape: a two tuple indicating query shape
    dropout_rate: a floating point number.
    image_shapes: optional tuple of integer scalars.
    name: an optional string.
    make_image_summary: Whether to make an attention image summary.
    dropout_broadcast_dims:  an optional list of integers less than 4
      specifying in which dimensions to broadcast the dropout decisions.
      saves memory.

  Returns:
    [batch, heads, height, width, depth] tensor, the output of attention.

  """
  if max_relative_position:
    raise ValueError("Relative local 2d attention not implemented")

  with tf.variable_scope(
      name,
      default_name="dot_product_unmasked_attention_local_2d_tpu",
      values=[q, k, v]):

    # This calculation only works for self attention.
    # q, k and v must therefore have the same shape.
    q.get_shape().assert_is_compatible_with(k.get_shape())
    q.get_shape().assert_is_compatible_with(v.get_shape())
    orig_q_shape = common_layers.shape_list(q)
    # Pad query, key, value to ensure multiple of corresponding lengths.
    memory_flange = [int(query_shape[0]//2), int(query_shape[1]//2)]
    q = pad_to_multiple_2d(q, query_shape)
    k = pad_to_multiple_2d(k, query_shape)
    v = pad_to_multiple_2d(v, query_shape)
    q_shape = common_layers.shape_list(q)
    (height, width) = (q_shape[2],
                       q_shape[3])
    _, num_heads, height, width, depth_k = common_layers.shape_list(k)
    depth_v = common_layers.shape_list(v)[-1]
    num_h_blocks = height//query_shape[0]
    num_w_blocks = width//query_shape[1]
    # Extract center queries, keys, and values
    q = tf.reshape(q, [-1, height, width, depth_k])
    queries = _extract_blocks(
        q, query_shape[0], query_shape[1])
    k = tf.reshape(k, [-1, height, width, depth_k])
    keys = get_2d_local_memory_v2(
        k, query_shape, memory_flange)
    v = tf.reshape(v, [-1, height, width, depth_v])
    values = get_2d_local_memory_v2(
        v, query_shape, memory_flange)
    memory_h = query_shape[0] + 2*memory_flange[0]
    memory_w = query_shape[1] + 2*memory_flange[1]
    queries = tf.reshape(queries, [-1, num_heads, num_h_blocks, num_w_blocks,
                                   query_shape[0]*query_shape[1], depth_k])
    keys = tf.reshape(keys, [-1, num_heads, num_h_blocks, num_w_blocks,
                             memory_h*memory_w, depth_k])
    values = tf.reshape(values, [-1, num_heads, num_h_blocks, num_w_blocks,
                                 memory_h*memory_w, depth_v])
    logits = tf.matmul(queries, keys, transpose_b=True)
    if bias is not None:
      logits += bias

    weights = tf.nn.softmax(logits, name="attention_weights")
    # Dropping out the attention links for each of the heads
    weights = common_layers.dropout_with_broadcast_dims(
        weights, 1.0 - dropout_rate, broadcast_dims=dropout_broadcast_dims)
    if common_layers.should_generate_summaries() and make_image_summary:
      attention_image_summary(weights, image_shapes)
    ret = tf.matmul(weights, values)
    # we need to get it back to shape [batch, heads, height, width]
    ret = tf.reshape(ret, [-1, num_heads, num_h_blocks, num_w_blocks,
                           query_shape[0], query_shape[1], depth_v])
    ret = tf.transpose(ret, [0, 1, 2, 4, 3, 5, 6])
    ret = tf.reshape(ret, [-1, num_heads, num_h_blocks*query_shape[0],
                           num_w_blocks*query_shape[1], depth_v])
    # slice if padding was introduced
    ret = tf.slice(ret, [0, 0, 0, 0, 0], [-1, -1, orig_q_shape[2],
                                          orig_q_shape[3], -1])
    return ret


def dot_product_unmasked_attention_local_2d_tpu_simple(
    x, bias, total_key_depth, total_value_depth, num_heads,
    query_shape=(8, 8),
    dropout_rate=0.0, image_shapes=None, make_image_summary=False,
    dropout_broadcast_dims=None):

  """Calculate simple unmasked dot-product local self-attention 2d on tpu.

  The query, key, and value blocks are the same. We do not do a second linear
  transformation after computing the values

  Args:
    x: a Tensor with shape [batch, height, width, depth].
    bias: bias Tensor.
    total_key_depth: the dimensions of the keys
    total_value_depth: the dimensions of the values
    num_heads: number of heads
    query_shape: a two tuple indicating query shape
    dropout_rate: a floating point number.
    image_shapes: optional tuple of integer scalars.
    make_image_summary: Whether to make an attention image summary.
    dropout_broadcast_dims:  an optional list of integers less than 4
      specifying in which dimensions to broadcast the dropout decisions.
      saves memory.

  Returns:
    ret: [batch, height, width, total_value_depth] tensor,
      the output of attention.
    q: [batch, height, width, total_key_depth] query tensor
    k: [batch, height, width, total_key_depth] key tensor
    v: [batch, height, width, total_value_depth] value tensor

  """
  # This calculation only works for self attention.
  # q, k and v must therefore have the same shape.
  orig_x_shape = common_layers.shape_list(x)
  # Pad query, key, value to ensure multiple of corresponding lengths if
  # necessary
  is_padded = False
  if (orig_x_shape[1]%query_shape[0]) != 0 or (
      orig_x_shape[2]%query_shape[1]) != 0:
    x = pad_to_multiple_2d(x, query_shape)
    is_padded = True
  _, height, width, depth = common_layers.shape_list(x)
  assert depth%num_heads == 0
  num_h_blocks = height//query_shape[0]
  num_w_blocks = width//query_shape[1]
  # Extract center queries, keys, and values
  x_blocks = _extract_blocks(x, query_shape[0], query_shape[1])
  x_blocks = tf.reshape(x_blocks, [-1, query_shape[0]*query_shape[1], depth])
  q, k, v = compute_qkv(x_blocks, None, total_key_depth, total_value_depth)
  hsplit = lambda x: split_heads(x, num_heads)
  q, k, v = map(hsplit, [q, k, v])
  logits = tf.matmul(q, k, transpose_b=True)
  if bias is not None:
    logits += bias
  weights = tf.nn.softmax(logits, name="attention_weights")
  # Dropping out the attention links for each of the heads
  weights = common_layers.dropout_with_broadcast_dims(
      weights, 1.0 - dropout_rate, broadcast_dims=dropout_broadcast_dims)
  if common_layers.should_generate_summaries() and make_image_summary:
    attention_image_summary(weights, image_shapes)
  output = tf.matmul(weights, v)
  output = combine_heads(output)
  # we need to get it back to shape [batch, height, width]
  ret = tf.reshape(output, [-1, num_h_blocks, num_w_blocks,
                            query_shape[0], query_shape[1], total_value_depth])

  ret = tf.transpose(ret, [0, 1, 3, 2, 4, 5])
  ret = tf.reshape(ret, [-1, num_h_blocks*query_shape[0],
                         num_w_blocks*query_shape[1], total_value_depth])
  # slice if padding was introduced
  if is_padded:
    ret = tf.slice(ret, [0, 0, 0, 0], [-1, orig_x_shape[1],
                                       orig_x_shape[2], -1])
  return ret, q, k, v


def masked_within_block_local_attention_1d(q, k, v, block_length=64, name=None):
  """Attention to the source and a neighborhood to the left within a block.

  The sequence is divided into blocks of length block_length. Attention for a
  given query position can only see memory positions less than or equal to the
  query position in the corresponding block.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    block_length: an integer
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]
  """
  with tf.variable_scope(
      name, default_name="within_local_attention_1d", values=[q, k, v]):
    batch, heads, length, depth_k = common_layers.shape_list(q)
    depth_v = common_layers.shape_list(v)[-1]
    if isinstance(block_length, tf.Tensor):
      const = contrib.util().constant_value(block_length)
      if const is not None:
        block_length = int(const)

    # Pad query, key, value to ensure multiple of block length.
    original_length = length
    padding_size = tf.mod(-length, block_length)
    length += padding_size
    padding = [[0, 0], [0, 0], [0, padding_size], [0, 0]]
    q = tf.pad(q, padding)
    k = tf.pad(k, padding)
    v = tf.pad(v, padding)

    # Compute attention for all subsequent query blocks.
    num_blocks = tf.div(length, block_length)
    q = tf.reshape(q, [batch, heads, num_blocks, block_length, depth_k])
    k = tf.reshape(k, [batch, heads, num_blocks, block_length, depth_k])
    v = tf.reshape(v, [batch, heads, num_blocks, block_length, depth_v])
    # [batch, heads, num_blocks, block_length, block_length]
    attention = tf.matmul(q, k, transpose_b=True)
    attention += tf.reshape(attention_bias_lower_triangle(block_length),
                            [1, 1, 1, block_length, block_length])
    attention = tf.nn.softmax(attention)
    # [batch, heads, num_blocks, block_length, depth_v]
    output = tf.matmul(attention, v)
    output = tf.reshape(output, [batch, heads, -1, depth_v])

    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape([None if isinstance(dim, tf.Tensor) else dim for dim in
                      (batch, heads, length, depth_v)])
    return output


def _relative_position_to_absolute_position_unmasked(x):
  """Converts tensor from relative to aboslute indexing for local attention.

  Args:
    x: a Tensor of shape [batch (or batch*num_blocks), heads,
                          length, 2 * length - 1]

  Returns:
    A Tensor of shape [batch (or batch*num_blocks), heads, length, length]
  """
  x_shape = common_layers.shape_list(x)
  batch = x_shape[0]
  heads = x_shape[1]
  length = x_shape[2]
  # Concat columns of pad to shift from relative to absolute indexing.
  col_pad = tf.zeros((batch, heads, length, 1))
  x = tf.concat([x, col_pad], axis=3)

  # Concat extra elements so to add up to shape (len+1, 2*len-1).
  flat_x = tf.reshape(x, [batch, heads, length * 2 * length])
  flat_pad = tf.zeros((batch, heads, length-1))
  flat_x_padded = tf.concat([flat_x, flat_pad], axis=2)

  # Reshape and slice out the padded elements.
  final_x = tf.reshape(flat_x_padded, [batch, heads, length+1, 2*length-1])
  final_x = final_x[:, :, :, length-1:]
  final_x = final_x[:, :, :length, :]
  return final_x


def masked_local_attention_1d(q,
                              k,
                              v,
                              block_length=128,
                              make_image_summary=False,
                              dropout_rate=0.,
                              name=None):
  """Attention to the source position and a neighborhood to the left of it.

  The sequence is divided into blocks of length block_length. Attention for a
  given query position can only see memory positions less than or equal to the
  query position, in the corresponding block and the previous block.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    block_length: an integer
    make_image_summary: a boolean, whether to make an attention image summary.
    dropout_rate: Dropout rate for attention dropout
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_attention_1d", values=[q, k, v]):
    batch, heads, length, depth_k = common_layers.shape_list(q)
    depth_v = common_layers.shape_list(v)[-1]
    if isinstance(block_length, tf.Tensor):
      const = contrib.util().constant_value(block_length)
      if const is not None:
        block_length = int(const)
    # If (length < 2 * block_length), then we use only one block.
    if isinstance(length, int) and isinstance(block_length, int):
      block_length = length if length < block_length * 2 else block_length
    else:
      block_length = tf.where(
          tf.less(length, block_length * 2), length, block_length)

    # Pad query, key, value to ensure multiple of block length.
    original_length = length
    padding_size = tf.mod(-length, block_length)
    length += padding_size
    padding = [[0, 0], [0, 0], [0, padding_size], [0, 0]]
    q = tf.pad(q, padding)
    k = tf.pad(k, padding)
    v = tf.pad(v, padding)

    if isinstance(length, int) and isinstance(block_length, int):
      num_blocks = length // block_length
    else:
      num_blocks = tf.div(length, block_length)

    # Compute attention for the first query block.
    first_q = tf.slice(q, [0, 0, 0, 0], [-1, -1, block_length, -1])
    first_k = tf.slice(k, [0, 0, 0, 0], [-1, -1, block_length, -1])
    first_v = tf.slice(v, [0, 0, 0, 0], [-1, -1, block_length, -1])

    first_output = dot_product_attention(
        first_q,
        first_k,
        first_v,
        attention_bias_lower_triangle(block_length),
        dropout_rate=dropout_rate,
        make_image_summary=make_image_summary,
        name="first_block")

    # Compute attention for all subsequent query blocks.
    q = tf.reshape(q, [batch, heads, num_blocks, block_length, depth_k])
    k = tf.reshape(k, [batch, heads, num_blocks, block_length, depth_k])
    v = tf.reshape(v, [batch, heads, num_blocks, block_length, depth_v])

    local_k = _make_local_block(k, depth_k, batch, heads, num_blocks,
                                block_length)
    local_v = _make_local_block(v, depth_v, batch, heads, num_blocks,
                                block_length)
    tail_q = tf.slice(q, [0, 0, 1, 0, 0], [-1, -1, -1, -1, -1])
    tail_q = tf.reshape(tail_q,
                        [batch, heads, num_blocks - 1, block_length, depth_k])
    local_length = common_layers.shape_list(local_k)[3]

    # make sure source_pos <= target_pos
    good_part = common_layers.ones_matrix_band_part(
        block_length,
        local_length,
        -1,
        block_length,
        out_shape=[1, 1, 1, block_length, local_length])
    bias = (1.0 - good_part) * -1e9
    # TODO(noam): figure out how to show a summary for the remaining blocks.
    # The naive way currently causes errors due to empty tensors.
    # output: [batch, heads, num_blocks-1, block_length, depth_v]
    tail_output = dot_product_attention(
        tail_q,
        local_k,
        local_v,
        bias,
        dropout_rate=dropout_rate,
        make_image_summary=False,
        name="tail_block")
    tail_output = tf.reshape(
        tail_output, [batch, heads, (num_blocks - 1) * block_length, depth_v])
    output = tf.concat([first_output, tail_output], axis=2)

    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output = tf.reshape(output, [batch, heads, original_length, depth_v])
    return output


def _make_local_block(x, depth, batch, heads, num_blocks, block_length):
  """Helper function to create a local version of the keys or values for 1d."""
  prev_block = tf.slice(x, [0, 0, 0, 0, 0],
                        [-1, -1, num_blocks - 1, -1, -1])
  cur_block = tf.slice(x, [0, 0, 1, 0, 0], [-1, -1, -1, -1, -1])
  local_block = tf.concat([prev_block, cur_block], 3)
  return tf.reshape(local_block,
                    [batch, heads, num_blocks - 1, block_length * 2, depth])


def masked_relative_local_attention_1d(q,
                                       k,
                                       v,
                                       block_length=128,
                                       make_image_summary=False,
                                       dropout_rate=0.,
                                       heads_share_relative_embedding=False,
                                       add_relative_to_values=False,
                                       name=None):
  """Masked local 1d attention with relative positions.

  The sequence is divided into blocks of length block_size.
  Attention for a given query position can only see memory positions
  less than or equal to the query position, in the corresponding block
  and the previous block.

  If mask_right is True, then a target position cannot see greater source
  positions.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    block_length: an integer
    make_image_summary: a boolean, whether to make an attention image summary.
    dropout_rate: Dropout rate for attention dropout
    heads_share_relative_embedding: a boolean for sharing relative embeddings.
    add_relative_to_values: a boolean for whether to add relative component to
        values.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]

  Raises:
    ValueError: wwhen the name for the variable scope is not passed.
  """
  if not name:
    raise ValueError("Name must be assigned since reuse for variable scope is "
                     "set to tf.AUTO_REUSE, in order to reuse relative "
                     "embeddings of keys and values.")

  # Reuse flag is set to auto_reuse to reuse relative embeddings of keys and
  # values across blocks (first and tail blocks).
  with tf.variable_scope(
      name, default_name="masked_relative_local_attention_1d",
      values=[q, k, v], reuse=tf.AUTO_REUSE):

    default_block_length = block_length
    batch = common_layers.shape_list(q)[0]
    heads = common_layers.shape_list(q)[1]
    length = common_layers.shape_list(q)[2]
    # If (length < 2 * block_length), then we use only one block.
    if isinstance(length, int) and isinstance(block_length, int):
      block_length = length if length < block_length * 2 else block_length
    else:
      block_length = tf.where(
          tf.less(length, block_length * 2), length, block_length)
    depth_k = common_layers.shape_list(k)[3]
    depth_v = common_layers.shape_list(v)[3]
    original_length = length
    padding_size = tf.mod(-length, block_length)
    length += padding_size
    padding = [[0, 0], [0, 0], [0, padding_size], [0, 0]]
    q = tf.pad(q, padding)
    k = tf.pad(k, padding)
    v = tf.pad(v, padding)

    num_blocks = length // block_length
    # compute attention for the first query block.
    first_q = tf.slice(q, [0, 0, 0, 0], [-1, -1, block_length, -1])
    first_k = tf.slice(k, [0, 0, 0, 0], [-1, -1, block_length, -1])
    first_v = tf.slice(v, [0, 0, 0, 0], [-1, -1, block_length, -1])
    # Relative embeddings will be used later as well.
    # TODO(avaswani,annahuang): check why 2*bl was breaking for music
    # Needs to be known at static shape inference time, hence cannot be
    # 2 * block_length.
    rel_embed_length = 4 * default_block_length
    # We only multiply with the needed embeddings as we slice them out.
    first_rel_embeddings = get_relative_embeddings_left(
        rel_embed_length, block_length, depth_k, heads,
        heads_share_relative_embedding, "relative_embeddings")
    first_rel_logits = matmul_with_relative_keys(
        first_q, first_rel_embeddings, heads_share_relative_embedding)
    first_logits = tf.matmul(first_q, first_k, transpose_b=True)
    first_logits += (
        _relative_position_to_absolute_position_masked(first_rel_logits))
    # adding a mask
    first_logits += (
        common_layers.cast_like(attention_bias_lower_triangle(block_length),
                                first_logits))
    first_att = tf.nn.softmax(first_logits,
                              name="first_attention_weights")
    # dropping out the attention links for each of the heads
    first_att = common_layers.dropout_with_broadcast_dims(
        first_att, 1.0 - dropout_rate,
        broadcast_dims=None)
    # only call image summary for the first block
    if common_layers.should_generate_summaries() and make_image_summary:
      attention_image_summary(first_att, None)
    first_output = tf.matmul(first_att, first_v)

    # compute attention for all subsequent query blocks.
    q = tf.reshape(q, [batch, heads, num_blocks, block_length, depth_k])
    k = tf.reshape(k, [batch, heads, num_blocks, block_length, depth_k])
    v = tf.reshape(v, [batch, heads, num_blocks, block_length, depth_v])
    local_k = _make_local_block(k, depth_k, batch, heads, num_blocks,
                                block_length)
    local_v = _make_local_block(v, depth_v, batch, heads, num_blocks,
                                block_length)
    tail_q = tf.slice(q, [0, 0, 1, 0, 0], [-1, -1, -1, -1, -1])
    tail_q = tf.reshape(tail_q,
                        [batch, heads, num_blocks - 1, block_length, depth_k])
    local_length = common_layers.shape_list(local_k)[3]

    # collapsing num blocks and batch size so that we can reuse
    # functions
    def _reshape_for_relative(x):
      x_shape = common_layers.shape_list(x)
      # [batch, num_blocks, heads, length, depth]
      x = tf.transpose(x, [0, 2, 1, 3, 4])
      x = tf.reshape(x, [batch*x_shape[2], heads, x_shape[3],
                         x_shape[4]])
      return x
    rel_tail_q = _reshape_for_relative(tail_q)
    rel_k = _reshape_for_relative(local_k)
    rel_v = _reshape_for_relative(local_v)
    rel_embeddings = get_relative_embeddings_left(
        rel_embed_length, 2 * block_length, depth_k, heads,
        heads_share_relative_embedding, "relative_embeddings")
    rel_logits = matmul_with_relative_keys(
        rel_tail_q, rel_embeddings, heads_share_relative_embedding)
    # Computing relative logits separately for the masked and unmasked parts
    # because the reshaping logic is different for both
    masked_rel_logits = tf.slice(rel_logits, [0, 0, 0, block_length],
                                 [-1, -1, -1, -1])
    masked_rel_logits = _relative_position_to_absolute_position_masked(
        masked_rel_logits)
    unmasked_rel_logits = tf.slice(rel_logits, [0, 0, 0, 0],
                                   [-1, -1, -1, 2*block_length-1])
    unmasked_rel_logits = _relative_position_to_absolute_position_unmasked(
        unmasked_rel_logits)
    all_rel_logits = tf.concat([unmasked_rel_logits, masked_rel_logits],
                               axis=3)
    all_logits = (
        tf.matmul(rel_tail_q, rel_k, transpose_b=True) + all_rel_logits)
    # make sure source_pos <= target_pos
    good_part = common_layers.ones_matrix_band_part(block_length,
                                                    local_length,
                                                    -1, block_length)
    mask = (1.0 - good_part) * -1e9
    mask = common_layers.cast_like(mask, all_logits)
    all_logits += tf.reshape(mask, [1, 1, block_length, local_length])
    weights = tf.nn.softmax(all_logits, name="attention_weights")
    # [batch (* num_blocks), heads, query_length (=block_length),
    # key_length (=2*block_length)]
    weights = common_layers.dropout_with_broadcast_dims(
        weights, 1.0 - dropout_rate,
        broadcast_dims=None)

    output = tf.matmul(weights, rel_v)
    if add_relative_to_values:
      # Adds the contribution of the weighted relative embeddings to the values.
      weights_for_unmasked, weights_for_masked = (
          tf.split(weights, 2, axis=3))
      rel_weights_unmasked = _absolute_position_to_relative_position_unmasked(
          weights_for_unmasked)
      rel_weights_masked = _absolute_position_to_relative_position_masked(
          weights_for_masked)

      value_rel_embeddings_unmasked = get_relative_embeddings_left(
          rel_embed_length, 2 * block_length, depth_v,
          heads, heads_share_relative_embedding,
          "value_relative_embeddings")
      # The unmasked part starts with index -1 as opposed 0 has take uptil last.
      if heads_share_relative_embedding:
        value_rel_embeddings_unmasked = value_rel_embeddings_unmasked[:-1, :]
      else:
        value_rel_embeddings_unmasked = value_rel_embeddings_unmasked[:, :-1, :]
      value_rel_embeddings_masked = get_relative_embeddings_left(
          rel_embed_length, block_length, depth_v,
          heads, heads_share_relative_embedding,
          "value_relative_embeddings")

      # [batch (*num_blocks), heads, query length, key length]
      rel_weights = tf.concat(
          [rel_weights_unmasked, rel_weights_masked], axis=3)
      if heads_share_relative_embedding:
        value_rel_embeddings_concat_axis = 0
      else:
        value_rel_embeddings_concat_axis = 1
      value_rel_embeddings = tf.concat(
          [value_rel_embeddings_unmasked, value_rel_embeddings_masked],
          axis=value_rel_embeddings_concat_axis)
      output_rel = matmul_with_relative_values(
          rel_weights, value_rel_embeddings, heads_share_relative_embedding)
      output += output_rel

    # bring to [batch, heads, num_blocks-1, block_length, depth]
    output = tf.reshape(output,
                        [batch, num_blocks-1, heads, block_length, depth_v])
    output = tf.transpose(output, [0, 2, 1, 3, 4])

    output = tf.reshape(
        output, [batch, heads, (num_blocks - 1) * block_length, depth_v])
    output = tf.concat([first_output, output], axis=2)
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output = tf.reshape(output, [batch, heads, original_length, depth_v])
    return output


def matmul_with_relative_values(x, y, heads_share_relative_embedding):
  if heads_share_relative_embedding:
    ret = tf.einsum("bhlm,md->bhld", x, y)
  else:
    ret = tf.einsum("bhlm,hmd->bhld", x, y)
  return ret


def matmul_with_relative_keys(x, y, heads_share_relative_embedding):
  if heads_share_relative_embedding:
    ret = tf.einsum("bhld,md->bhlm", x, y)
  else:
    ret = tf.einsum("bhld,hmd->bhlm", x, y)
  return ret


def local_attention_1d(q, k, v, block_length=128, filter_width=100, name=None):
  """Strided block local self-attention.

  The sequence is divided into blocks of length block_length. Attention for a
  given query position can see all memory positions in the corresponding block
  and filter_width many positions to the left and right of the block.

  Args:
    q: a Tensor with shape [batch, heads, length, depth_k]
    k: a Tensor with shape [batch, heads, length, depth_k]
    v: a Tensor with shape [batch, heads, length, depth_v]
    block_length: an integer
    filter_width: an integer indicating how much to look left and right of the
      block.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_self_attention_1d", values=[q, k, v]):
    # Check that q, k, v have the same shape except in their depth dimension.
    q.get_shape()[:-1].assert_is_compatible_with(k.get_shape()[:-1])
    q.get_shape()[:-1].assert_is_compatible_with(v.get_shape()[:-1])

    batch_size, num_heads, original_length, _ = common_layers.shape_list(q)

    # Pad query, key, value to ensure multiple of corresponding lengths.
    def pad_to_multiple(x, pad_length):
      x_length = common_layers.shape_list(x)[2]
      return tf.pad(x, [[0, 0], [0, 0], [0, -x_length % pad_length], [0, 0]])

    def pad_l_and_r(x, pad_length):
      return tf.pad(x, [[0, 0], [0, 0], [pad_length, pad_length], [0, 0]])

    # Set up query blocks.
    # [batch, heads, blocks_q, block_length, depth_k]
    q = pad_to_multiple(q, block_length)
    q = reshape_by_blocks(q, common_layers.shape_list(q), block_length)
    total_query_blocks = common_layers.shape_list(q)[2]

    # Set up key and value blocks.
    # [batch, heads, blocks_k, block_length, depth_k]
    blocks_per_filter_width = filter_width // block_length
    remaining_items = filter_width % block_length
    k = pad_to_multiple(k, block_length)
    v = pad_to_multiple(v, block_length)
    k = pad_l_and_r(k, filter_width + block_length - remaining_items)
    v = pad_l_and_r(v, filter_width + block_length - remaining_items)
    k = reshape_by_blocks(k, common_layers.shape_list(k), block_length)
    v = reshape_by_blocks(v, common_layers.shape_list(v), block_length)

    total_kv_blocks = common_layers.shape_list(k)[2]

    slices = []
    # prepare the left-most and right-most partial blocks if needed
    if remaining_items:
      first_partial_block_k = tf.slice(
          k, [0, 0, 0, block_length - remaining_items, 0],
          [-1, -1, total_query_blocks, -1, -1])
      first_partial_block_v = tf.slice(
          v, [0, 0, 0, block_length - remaining_items, 0],
          [-1, -1, total_query_blocks, -1, -1])
      last_partial_block_k = tf.slice(
          k, [0, 0, total_kv_blocks - total_query_blocks, 0, 0],
          [-1, -1, -1, remaining_items, -1])
      last_partial_block_v = tf.slice(
          v, [0, 0, total_kv_blocks - total_query_blocks, 0, 0],
          [-1, -1, -1, remaining_items, -1])
      slices.append((first_partial_block_k, first_partial_block_v))
      slices.append((last_partial_block_k, last_partial_block_v))

    # Prepare the rest of the blocks
    first_block_index = 1 if remaining_items else 0
    attention_blocks = 2 * blocks_per_filter_width + 1
    for i in range(first_block_index, attention_blocks + first_block_index):
      block_k = tf.slice(k, [0, 0, i, 0, 0],
                         [-1, -1, total_query_blocks, -1, -1])
      block_v = tf.slice(v, [0, 0, i, 0, 0],
                         [-1, -1, total_query_blocks, -1, -1])
      slices.append((block_k, block_v))
    # [batch, heads, blocks_q, block_length + 2 * filter_width, depth_k]
    k = tf.concat([s[0] for s in slices], axis=3)
    v = tf.concat([s[1] for s in slices], axis=3)

    attention_bias = tf.expand_dims(embedding_to_padding(k) * -1e9, axis=-2)
    depth_v = common_layers.shape_list(v)[-1]

    output = dot_product_attention(
        q,
        k,
        v,
        attention_bias,
        dropout_rate=0.,
        name="local_1d",
        make_image_summary=False)
    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])

    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape([None if isinstance(dim, tf.Tensor) else dim for dim in
                      (batch_size, num_heads, original_length, depth_v)])
    return output


def reshape_by_blocks(x, x_shape, memory_block_size):
  """Reshapes input by splitting its length over blocks of memory_block_size.

  Args:
    x: a Tensor with shape [batch, heads, length, depth]
    x_shape: tf.TensorShape of x.
    memory_block_size: Integer which divides length.

  Returns:
    Tensor with shape
    [batch, heads, length // memory_block_size, memory_block_size, depth].
  """
  x = tf.reshape(x, [
      x_shape[0], x_shape[1], x_shape[2] // memory_block_size,
      memory_block_size, x_shape[3]
  ])
  return x


def dilated_self_attention_1d(q,
                              k,
                              v,
                              query_block_size=128,
                              memory_block_size=128,
                              gap_size=2,
                              num_memory_blocks=2,
                              name=None):
  """Dilated self-attention.

  Args:
    q: a Tensor with shape [batch, heads, length, depth]
    k: a Tensor with shape [batch, heads, length, depth]
    v: a Tensor with shape [batch, heads, length, depth]
    query_block_size: an integer indicating size of query block
    memory_block_size: an integer indicating the size of a memory block.
    gap_size: an integer indicating the gap size
    num_memory_blocks: how many memory blocks to look at to the left and right.
      Each will be separated by gap_size.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth]
  """
  with tf.variable_scope(
      name, default_name="dilated_self_attention_1d", values=[q, k, v]):
    v_list_shape = v.get_shape().as_list()
    assert v_list_shape == k.shape.as_list(), "K and V depths must be equal"
    v_shape = common_layers.shape_list(v)
    depth_v = v_shape[3]
    batch_size = v_shape[0]
    num_heads = v_shape[1]
    original_length = common_layers.shape_list(q)[2]

    # Pad query, key, value to ensure multiple of corresponding lengths.
    def pad_to_multiple(x, pad_length):
      x_length = common_layers.shape_list(x)[2]
      return tf.pad(x, [[0, 0], [0, 0], [0, -x_length % pad_length], [0, 0]])

    def pad_l_and_r(x, pad_length):
      return tf.pad(x, [[0, 0], [0, 0], [pad_length, pad_length], [0, 0]])

    q = pad_to_multiple(q, query_block_size)
    v = pad_to_multiple(v, query_block_size)
    k = pad_to_multiple(k, query_block_size)

    # Set up query blocks.
    new_q_shape = common_layers.shape_list(q)
    q = reshape_by_blocks(q, new_q_shape, query_block_size)
    self_k_part = reshape_by_blocks(k, new_q_shape, query_block_size)
    self_v_part = reshape_by_blocks(v, new_q_shape, query_block_size)

    # Set up key and value windows.
    k_v_padding = (gap_size + memory_block_size) * num_memory_blocks
    k = pad_l_and_r(k, k_v_padding)
    v = pad_l_and_r(v, k_v_padding)

    # Get gather indices.
    index_length = (new_q_shape[2] - query_block_size + memory_block_size)
    indices = tf.range(0, index_length, delta=1, name="index_range")
    indices = tf.reshape(indices, [1, -1, 1])  # [1, length, 1] for convs
    kernel = tf.expand_dims(tf.eye(memory_block_size), axis=1)
    gather_indices = tf.nn.conv1d(
        tf.cast(indices, tf.float32),
        kernel,
        query_block_size,
        padding="VALID",
        name="gather_conv")

    gather_indices = tf.squeeze(tf.cast(gather_indices, tf.int32), axis=0)

    # Get left and right memory blocks for each query.
    # [length, batch, heads, dim]
    k_t = tf.transpose(k, [2, 0, 1, 3])
    v_t = tf.transpose(v, [2, 0, 1, 3])
    left_k = gather_dilated_memory_blocks(
        k_t[:-k_v_padding, :, :, :], num_memory_blocks, gap_size,
        query_block_size, memory_block_size, gather_indices)
    left_v = gather_dilated_memory_blocks(
        v_t[:-k_v_padding, :, :, :], num_memory_blocks, gap_size,
        query_block_size, memory_block_size, gather_indices)

    right_k = gather_dilated_memory_blocks(
        k_t[k_v_padding:, :, :, :],
        num_memory_blocks,
        gap_size,
        query_block_size,
        memory_block_size,
        gather_indices,
        direction="right")
    right_v = gather_dilated_memory_blocks(
        v_t[k_v_padding:, :, :, :],
        num_memory_blocks,
        gap_size,
        query_block_size,
        memory_block_size,
        gather_indices,
        direction="right")

    k_windows = tf.concat([left_k, self_k_part, right_k], axis=3)
    v_windows = tf.concat([left_v, self_v_part, right_v], axis=3)
    attention_bias = tf.expand_dims(
        embedding_to_padding(k_windows) * -1e9, axis=-2)

    output = dot_product_attention(
        q,
        k_windows,
        v_windows,
        attention_bias,
        dropout_rate=0.,
        name="dilated_1d",
        make_image_summary=False)
    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])

    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape(v_list_shape)
    return output


def gather_dilated_memory_blocks(x,
                                 num_memory_blocks,
                                 gap_size,
                                 query_block_size,
                                 memory_block_size,
                                 gather_indices,
                                 direction="left"):
  """Gathers blocks with gaps in between.

  Args:
    x: Tensor of shape [length, batch, heads, depth]
    num_memory_blocks: how many memory blocks to look in "direction". Each will
      be separated by gap_size.
    gap_size: an integer indicating the gap size
    query_block_size: an integer indicating size of query block
    memory_block_size: an integer indicating the size of a memory block.
    gather_indices: The indices to gather from.
    direction: left or right

  Returns:
    Tensor of shape [batch, heads, blocks, block_length, depth]
  """
  gathered_blocks = []
  # gathering memory blocks
  for block_id in range(num_memory_blocks):
    block_end_index = -(query_block_size + gap_size *
                        (block_id + 1) + memory_block_size * block_id)
    block_start_index = (
        (memory_block_size + gap_size) * (num_memory_blocks - (block_id + 1)))
    if direction != "left":
      [block_end_index,
       block_start_index] = [-block_start_index, -block_end_index]
    if block_end_index == 0:
      x_block = x[block_start_index:]
    else:
      x_block = x[block_start_index:block_end_index]

    def gather_dilated_1d_blocks(x, gather_indices):
      x_new = tf.gather(x, gather_indices)
      # [batch, heads, blocks, block_length, dim]
      return tf.transpose(x_new, [2, 3, 0, 1, 4])

    gathered_blocks.append(gather_dilated_1d_blocks(x_block, gather_indices))
  return tf.concat(gathered_blocks, 3)


def masked_dilated_self_attention_1d(q,
                                     k,
                                     v,
                                     query_block_size=64,
                                     memory_block_size=64,
                                     gap_size=2,
                                     num_memory_blocks=2,
                                     name=None):
  """Dilated self-attention. TODO(avaswani): Try it and write a paper on it.

  Args:
    q: a Tensor with shape [batch, heads, length, depth]
    k: a Tensor with shape [batch, heads, length, depth]
    v: a Tensor with shape [batch, heads, length, depth]
    query_block_size: an integer
    memory_block_size: an integer indicating how much to look left.
    gap_size: an integer indicating the gap size
    num_memory_blocks: how many memory blocks to look at to the left. Each will
      be separated by gap_size.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, length, depth]
  """
  with tf.variable_scope(
      name, default_name="masked_dilated_self_attention_1d", values=[q, k, v]):
    v_list_shape = v.get_shape().as_list()
    assert v_list_shape == k.shape.as_list(), "K and V depths must be equal"
    v_shape = common_layers.shape_list(v)
    depth_v = v_shape[3]
    batch_size = v_shape[0]
    num_heads = v_shape[1]
    original_length = common_layers.shape_list(q)[2]

    # Pad query, key, value to ensure multiple of corresponding lengths.
    def pad_to_multiple(x, pad_length):
      x_length = common_layers.shape_list(x)[2]
      return tf.pad(x, [[0, 0], [0, 0], [0, -x_length % pad_length], [0, 0]])

    def pad_l(x, left_pad_length):
      return tf.pad(x, [[0, 0], [0, 0], [left_pad_length, 0], [0, 0]])

    q = pad_to_multiple(q, query_block_size)
    v = pad_to_multiple(v, query_block_size)
    k = pad_to_multiple(k, query_block_size)

    # Set up query blocks.
    new_q_shape = common_layers.shape_list(q)
    q = reshape_by_blocks(q, new_q_shape, query_block_size)

    # Set up key and value windows.
    self_k_part = reshape_by_blocks(k, new_q_shape, query_block_size)
    self_v_part = reshape_by_blocks(v, new_q_shape, query_block_size)
    k_v_padding = (gap_size + memory_block_size) * num_memory_blocks
    k = pad_l(k, k_v_padding)
    v = pad_l(v, k_v_padding)

    # Get gather indices.
    index_length = (new_q_shape[2] - query_block_size + memory_block_size)

    indices = tf.range(0, index_length, delta=1, name="index_range")
    indices = tf.reshape(indices, [1, -1, 1])  # [1, length, 1] for convs
    kernel = tf.expand_dims(tf.eye(memory_block_size), axis=1)
    gather_indices = tf.nn.conv1d(
        tf.cast(indices, tf.float32),
        kernel,
        query_block_size,
        padding="VALID",
        name="gather_conv")
    gather_indices = tf.squeeze(tf.cast(gather_indices, tf.int32), axis=0)

    # Get left and right memory blocks for each query.
    # [length, batch, heads, dim]
    k_t = tf.transpose(k, [2, 0, 1, 3])
    v_t = tf.transpose(v, [2, 0, 1, 3])

    k_unmasked_windows = gather_dilated_memory_blocks(
        k_t, num_memory_blocks, gap_size, query_block_size, memory_block_size,
        gather_indices)
    v_unmasked_windows = gather_dilated_memory_blocks(
        v_t, num_memory_blocks, gap_size, query_block_size, memory_block_size,
        gather_indices)

    # Combine memory windows.
    block_q_shape = common_layers.shape_list(q)
    masked_attention_bias = tf.tile(
        tf.expand_dims(attention_bias_lower_triangle(query_block_size), axis=0),
        [block_q_shape[0], block_q_shape[1], block_q_shape[2], 1, 1])
    padding_attention_bias = tf.expand_dims(
        embedding_to_padding(k_unmasked_windows) * -1e9, axis=-2)
    padding_attention_bias = tf.tile(padding_attention_bias,
                                     [1, 1, 1, query_block_size, 1])
    attention_bias = tf.concat(
        [masked_attention_bias, padding_attention_bias], axis=-1)
    # combine memory windows
    k_windows = tf.concat([self_k_part, k_unmasked_windows], 3)
    v_windows = tf.concat([self_v_part, v_unmasked_windows], 3)
    output = dot_product_attention(
        q,
        k_windows,
        v_windows,
        attention_bias,
        dropout_rate=0.,
        name="dilated_1d",
        make_image_summary=False)
    output = tf.reshape(output, [batch_size, num_heads, -1, depth_v])

    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0], [-1, -1, original_length, -1])
    output.set_shape(v_list_shape)
    return output


def local_attention_2d(q,
                       k,
                       v,
                       query_shape=(8, 16),
                       memory_flange=(8, 16),
                       name=None):
  """Strided block local self-attention.

  The 2-D sequence is divided into 2-D blocks of shape query_shape. Attention
  for a given query position can only see memory positions less than or equal to
  the query position. The memory positions are the corresponding block with
  memory_flange many positions to add to the height and width of the block
  (namely, left, top, and right).

  Args:
    q: a Tensor with shape [batch, heads, h, w, depth_k]
    k: a Tensor with shape [batch, heads, h, w, depth_k]
    v: a Tensor with shape [batch, heads, h, w, depth_v]. In the current
      implementation, depth_v must be equal to depth_k.
    query_shape: an tuple indicating the height and width of each query block.
    memory_flange: an integer indicating how much to look in height and width
      from each query block.
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, h, w, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_self_attention_2d", values=[q, k, v]):
    v_shape = common_layers.shape_list(v)

    # Pad query, key, value to ensure multiple of corresponding lengths.
    q = pad_to_multiple_2d(q, query_shape)
    k = pad_to_multiple_2d(k, query_shape)
    v = pad_to_multiple_2d(v, query_shape)
    paddings = [[0, 0], [0, 0], [memory_flange[0], memory_flange[1]],
                [memory_flange[0], memory_flange[1]], [0, 0]]
    k = tf.pad(k, paddings)
    v = tf.pad(v, paddings)

    # Set up query blocks.
    q_indices = gather_indices_2d(q, query_shape, query_shape)
    q_new = gather_blocks_2d(q, q_indices)

    # Set up key and value blocks.
    memory_shape = (query_shape[0] + 2 * memory_flange[0],
                    query_shape[1] + 2 * memory_flange[1])
    k_and_v_indices = gather_indices_2d(k, memory_shape, query_shape)
    k_new = gather_blocks_2d(k, k_and_v_indices)
    v_new = gather_blocks_2d(v, k_and_v_indices)

    attention_bias = tf.expand_dims(
        to_float(embedding_to_padding(k_new)) * -1e9, axis=-2)
    output = dot_product_attention(
        q_new,
        k_new,
        v_new,
        attention_bias,
        dropout_rate=0.,
        name="local_2d",
        make_image_summary=False)
    # Put representations back into original shapes.
    padded_q_shape = common_layers.shape_list(q)
    output = scatter_blocks_2d(output, q_indices, padded_q_shape)

    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0, 0],
                      [-1, -1, v_shape[2], v_shape[3], -1])
    return output


def pad_to_multiple_2d(x, block_shape):
  """Making sure x is a multiple of shape.

  Args:
    x: a [batch, heads, h, w, depth] or [batch, h, w, depth] tensor
    block_shape: a 2-d list of integer shapes

  Returns:
    padded_x: a [batch, heads, h, w, depth] or [batch, h, w, depth] tensor
  """
  old_shape = x.get_shape().dims
  last = old_shape[-1]
  if len(old_shape) == 4:
    height_padding = -common_layers.shape_list(x)[1] % block_shape[0]
    width_padding = -common_layers.shape_list(x)[2] % block_shape[1]
    paddings = [[0, 0], [0, height_padding], [0, width_padding], [0, 0]]
  elif len(old_shape) == 5:
    height_padding = -common_layers.shape_list(x)[2] % block_shape[0]
    width_padding = -common_layers.shape_list(x)[3] % block_shape[1]
    paddings = [[0, 0], [0, 0], [0, height_padding], [0, width_padding], [0, 0]]

  padded_x = tf.pad(x, paddings)
  padded_shape = padded_x.get_shape().as_list()
  padded_shape = padded_shape[:-1] + [last]
  padded_x.set_shape(padded_shape)
  return padded_x


def reshape_range(tensor, i, j, shape):
  """Reshapes a tensor between dimensions i and j."""
  t_shape = common_layers.shape_list(tensor)
  target_shape = t_shape[:i] + shape + t_shape[j:]
  return tf.reshape(tensor, target_shape)


def gather_blocks_2d(x, indices):
  """Gathers flattened blocks from x."""
  x_shape = common_layers.shape_list(x)
  x = reshape_range(x, 2, 4, [tf.reduce_prod(x_shape[2:4])])
  # [length, batch, heads, dim]
  x_t = tf.transpose(x, [2, 0, 1, 3])
  x_new = tf.gather(x_t, indices)
  # returns [batch, heads, num_blocks, block_length ** 2, dim]
  return tf.transpose(x_new, [2, 3, 0, 1, 4])


def scatter_blocks_2d(x, indices, shape):
  """scatters blocks from x into shape with indices."""
  x_shape = common_layers.shape_list(x)
  # [length, batch, heads, dim]
  x_t = tf.transpose(
      tf.reshape(x, [x_shape[0], x_shape[1], -1, x_shape[-1]]), [2, 0, 1, 3])
  x_t_shape = common_layers.shape_list(x_t)
  indices = tf.reshape(indices, [-1, 1])
  scattered_x = tf.scatter_nd(indices, x_t, x_t_shape)
  scattered_x = tf.transpose(scattered_x, [1, 2, 0, 3])
  return tf.reshape(scattered_x, shape)


def gather_indices_2d(x, block_shape, block_stride):
  """Getting gather indices."""
  # making an identity matrix kernel
  kernel = tf.eye(block_shape[0] * block_shape[1])
  kernel = reshape_range(kernel, 0, 1, [block_shape[0], block_shape[1], 1])
  # making indices [1, h, w, 1] to appy convs
  x_shape = common_layers.shape_list(x)
  indices = tf.range(x_shape[2] * x_shape[3])
  indices = tf.reshape(indices, [1, x_shape[2], x_shape[3], 1])
  indices = tf.nn.conv2d(
      tf.cast(indices, tf.float32),
      kernel,
      strides=[1, block_stride[0], block_stride[1], 1],
      padding="VALID")
  # making indices [num_blocks, dim] to gather
  dims = common_layers.shape_list(indices)[:3]
  if all([isinstance(dim, int) for dim in dims]):
    num_blocks = functools.reduce(operator.mul, dims, 1)
  else:
    num_blocks = tf.reduce_prod(dims)
  indices = tf.reshape(indices, [num_blocks, -1])
  return tf.cast(indices, tf.int32)


def make_2d_block_raster_mask(query_shape, memory_flange):
  """Creates a mask for 2d block raster scan.

  The query mask can look to the left, top left, top, and top right, but
  not to the right. Inside the query, we have the standard raster scan
  masking.
  Args:
    query_shape: A tuple of ints (query_height, query_width)
    memory_flange: A tuple of ints
      (memory_flange_height, memory_flange_width)

  Returns:
    A tensor of shape query_size, memory_size
  """
  # mask inside the query block
  query_triangle = common_layers.ones_matrix_band_part(
      np.prod(query_shape), np.prod(query_shape), -1, 0)
  split_query_masks = tf.split(query_triangle, query_shape[0], axis=1)
  # adding mask for left and right
  mask_pieces = [
      tf.concat(  # pylint: disable=g-complex-comprehension
          [tf.ones([np.prod(query_shape), memory_flange[1]]),
           split_query_masks[i],
           tf.zeros([np.prod(query_shape), memory_flange[1]])],
          axis=1) for i in range(query_shape[0])
  ]
  # adding mask for top
  final_mask = tf.concat(
      [
          tf.ones([
              np.prod(query_shape),
              (query_shape[1] + 2 * memory_flange[1]) * memory_flange[0]
          ]),
          tf.concat(mask_pieces, axis=1)
      ],
      axis=1)
  # 0.0 is visible location, 1.0 is masked.
  return 1. - final_mask


def get_memory_region(x, query_block_shape, memory_flange, q_indices):
  """Get the memory regions that surround a 2d query.

    The memory regions will be the left and top right.

  Args:
    x: A tensor with shape [batch, heads, height, width, depth]
    query_block_shape: a 2-d tuple of integers
    memory_flange: a 2-d tuple of integers
    q_indices: a tensor of indices for each of the center blocks.
      [num_blocks, block_length]
  Returns:
    x_flange: A tensor of shape [batch, heads, #blocks, block_length, depth]
  """
  # Padding x to be multiple of query_shape and then
  # extracting the memory blocks from the same regions as the query blocks
  x_query_padded = pad_to_multiple_2d(x, query_block_shape)
  x_center = gather_blocks_2d(x_query_padded, q_indices)
  # Then padding the flange region
  paddings = [[0, 0], [0, 0], [memory_flange[0], 0],
              [memory_flange[1], memory_flange[1]], [0, 0]]
  x_memory_padded = tf.pad(x_query_padded, paddings)
  left_x = None
  top_x = None
  # Extracting the memory regions around the query block. left_x_region extends
  # to the left and the top_x_region is the combination of top left, top, and
  # top right of the query block
  # if no left region
  if memory_flange[1] > 0:
    left_x_region = x_memory_padded[:, :, memory_flange[
        0]:, :-(query_block_shape[1] + memory_flange[1]), :]
    left_memory_shape = (query_block_shape[0], memory_flange[1])
    left_indices = gather_indices_2d(left_x_region, left_memory_shape,
                                     query_block_shape)
    left_x = gather_blocks_2d(left_x_region, left_indices)
  # if no top region
  if memory_flange[0] > 0:
    top_x_region = x_memory_padded[:, :, :-query_block_shape[0], :, :]

    top_memory_shape = (memory_flange[0],
                        query_block_shape[1] + 2 * memory_flange[1])

    top_indices = gather_indices_2d(top_x_region, top_memory_shape,
                                    query_block_shape)

    top_x = gather_blocks_2d(top_x_region, top_indices)
  x_flange = None
  if top_x is not None and left_x is not None:
    x_flange = tf.concat([top_x, left_x], axis=3)
  else:
    x_flange = top_x if top_x is not None else left_x
  return x_flange, x_center


def get_shifted_center_blocks(x, indices):
  """Get right shifted blocks for masked local attention 2d.

  Args:
    x: A tensor with shape [batch, heads, height, width, depth]
    indices: The indices to gather blocks

  Returns:
    x_shifted: a tensor of extracted blocks, each block right shifted along
      length.
  """
  center_x = gather_blocks_2d(x, indices)

  # Shift right along the length dimension
  def shift_right_2d_blocks(x):
    """Shift the second to last dimension of x right by one."""
    shifted_targets = (
        tf.pad(x, [[0, 0], [0, 0], [0, 0], [1, 0], [0, 0]])[:, :, :, :-1, :])
    return shifted_targets

  x_shifted = shift_right_2d_blocks(center_x)
  return x_shifted


def right_shift_blockwise(x, query_shape, name=None):
  """Right shifts once in every block.

  Args:
    x: a tensor of shape [batch, height, width, depth]
    query_shape: A 2d tuple of ints
    name: a string

  Returns:
    output: a tensor of the same shape as x
  """
  with tf.variable_scope(
      name, default_name="right_shift_blockwise", values=[x]):
    x_list_shape = x.get_shape().as_list()
    x_shape = common_layers.shape_list(x)
    # Add a dummy dimension for heads.
    x = tf.expand_dims(x, axis=1)
    x = pad_to_multiple_2d(x, query_shape)
    padded_x_shape = common_layers.shape_list(x)
    # Set up q blocks.
    x_indices = gather_indices_2d(x, query_shape, query_shape)
    x_new = get_shifted_center_blocks(x, x_indices)

    # Put representations back into original shapes.
    output = scatter_blocks_2d(x_new, x_indices, padded_x_shape)
    # Remove the dummy head dimension.
    output = tf.squeeze(output, axis=1)
    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0], [-1, x_shape[1], x_shape[2], -1])
    output.set_shape(x_list_shape)
    return output


def right_shift_blockwise_nd(x, block_shape):
  """Right shift once in every block.

  Args:
    x: a [batch, d1, d2, ..., dn, depth] tensor
    block_shape: a tuple (q1, q2, ..., qn) representing the block shape

  Returns:
    a [batch, d1, d2, ..., dn, depth] tensor, right shifted.
  """
  blocked_x = break_into_blocks_nd(x, block_shape)
  blocked_x_shape = common_layers.shape_list(blocked_x)
  blocked_x = tf.reshape(blocked_x,
                         [blocked_x_shape[0], -1, blocked_x_shape[-1]])
  padded_x = tf.pad(blocked_x, [[0, 0], [1, 0], [0, 0]])
  x = tf.slice(padded_x, [0, 0, 0],
               [-1, np.prod(blocked_x_shape[1:-1], dtype=np.int32), -1])
  x = tf.reshape(x, blocked_x_shape)
  return put_back_blocks_nd(x, block_shape)


def masked_local_attention_2d(q,
                              k,
                              v,
                              query_shape=(8, 16),
                              memory_flange=(8, 16),
                              name=None):
  """Strided block local self-attention.

  Each position in a query block can attend to all the generated queries in
  the query block, which are generated in raster scan, and positions that are
  generated to the left and top. The shapes are specified by query shape and
  memory flange. Note that if you're using this function, you do not need to
  right shift. Right shifting happens inside this function separately for each
  block.

  Args:
    q: a Tensor with shape [batch, heads, h, w, depth_k]
    k: a Tensor with shape [batch, heads, h, w, depth_k]
    v: a Tensor with shape [batch, heads, h, w, depth_v]. In the current
      implementation, depth_v must be equal to depth_k.
    query_shape: an tuple indicating the height and width of each query block.
      query_shape = block_shape
    memory_flange: an integer indicating how much to look in height and width
      from each query block.
      memory shape = query_shape + (block_flange[0], 2*block_flange[1])
    name: an optional string

  Returns:
    a Tensor of shape [batch, heads, h, w, depth_v]
  """
  with tf.variable_scope(
      name, default_name="local_masked_self_attention_2d", values=[q, k, v]):
    v_shape = common_layers.shape_list(v)

    # Pad query to ensure multiple of corresponding lengths.
    q = pad_to_multiple_2d(q, query_shape)

    # Set up query blocks.
    q_indices = gather_indices_2d(q, query_shape, query_shape)
    q_new = gather_blocks_2d(q, q_indices)

    # Set up key and value blocks.
    k_flange, k_center = get_memory_region(k, query_shape, memory_flange,
                                           q_indices)
    v_flange, v_center = get_memory_region(v, query_shape, memory_flange,
                                           q_indices)
    if k_flange is not None:
      k_new = tf.concat([k_flange, k_center], axis=3)
      v_new = tf.concat([v_flange, v_center], axis=3)
    else:
      k_new = k_center
      v_new = v_center

    # Set up the masks.
    query_elements = np.prod(query_shape)
    padding_mask = None
    if k_flange is not None:
      padding_mask = tf.expand_dims(
          embedding_to_padding(k_flange) * -1e9, axis=-2)
      padding_mask = tf.tile(padding_mask, [1, 1, 1, query_elements, 1])

    center_attention_bias = attention_bias_lower_triangle(
        np.prod(query_elements))
    center_attention_bias = tf.reshape(
        center_attention_bias, [1, 1, 1, query_elements, query_elements])
    v_center_shape = common_layers.shape_list(v_center)
    center_attention_bias = tf.tile(
        center_attention_bias,
        [v_center_shape[0], v_center_shape[1], v_center_shape[2], 1, 1])
    if padding_mask is not None:
      # Combine the mask for padding and visible region.
      attention_bias = tf.concat([padding_mask, center_attention_bias], axis=4)
    else:
      attention_bias = center_attention_bias

    output = dot_product_attention(
        q_new,
        k_new,
        v_new,
        attention_bias,
        dropout_rate=0.,
        name="masked_local_2d",
        make_image_summary=False)
    # Put representations back into original shapes.
    padded_q_shape = common_layers.shape_list(q)
    output = scatter_blocks_2d(output, q_indices, padded_q_shape)

    # Remove the padding if introduced.
    output = tf.slice(output, [0, 0, 0, 0, 0],
                      [-1, -1, v_shape[2], v_shape[3], -1])
    return output


def masked_local_attention_nd(q,
                              k,
                              v,
                              query_shape,
                              memory_flange,
                              decode_step=None,
                              name=None):
  """Masked local attention nd.

  Each position in q can attend to positions in memory that are positioned less
  than or equal to query position according to raster scan ordering and are in
  the same memory block. A memory block is n-dimensional and each dimension 'i'
  is of size q[i] + 2 * m[i] except for the first dimension which is of size
  q[0] + m[0]. NOTE: This computation assumes memory_flange is divisible by
  query_shape in every dimension.

  Args:
    q: a [batch, heads, d1, d2, ..., dn, depth_k] tensor or a [batch, heads, 1,
      1, ..., 1, depth_k] tensor in decoding mode.
    k: a [batch, heads, d1, d2, ..., dn, depth_k] tensor
    v: a [batch, heads, d1, d2, ..., dn, depth_v] tensor
    query_shape: a tuple (q1, q2, ..., qn) indicating the shape of query blocks.
    memory_flange: a tuple (m1, m2, ..., mn) indicating the number of extra
      positions in the attention memory. memory_shape=[q1 + m1, d2 + 2 * m2,
      ..., dn + 2 * mn]
    decode_step: an integer in fast decoding mode.
    name: an optional string

  Returns:
    a [batch, head, d1, d2, ..., dn, depth_v] tensor or
      [batch, head, 1, 1, ..., 1, depth_v] if decode_step is not None.
  """
  assert all([m % b == 0 for m, b in zip(memory_flange, query_shape)])
  with tf.variable_scope(
      name, default_name="masked_local_attention_nd", values=[q, k, v]):
    # This computation only applies to self attention, so assert q, k and v have
    # the same dimensions.
    if decode_step is None:
      q.get_shape().assert_is_compatible_with(k.get_shape())
      q.get_shape()[:-1].assert_is_compatible_with(v.get_shape()[:-1])
    else:
      k.get_shape().assert_is_compatible_with(v.get_shape())

    # move heads to batch dimension. This is needed to reduce number of
    # dimensions as much as possible, since most ops support only up to 7
    # dimensions.
    q_shape = common_layers.shape_list(q)
    k_shape = common_layers.shape_list(k)
    v_shape = common_layers.shape_list(v)
    q = tf.reshape(q, [-1] + q_shape[2:])
    k = tf.reshape(k, [-1] + k_shape[2:])
    v = tf.reshape(v, [-1] + v_shape[2:])

    # Pad query, key, value to ensure multiple of corresponding lengths.
    if decode_step is None:
      # don't pad query in fast decoding mode. We only need to calculate self
      # attention for one position.
      q = pad_to_multiple_nd(q, query_shape)
    k = pad_to_multiple_nd(k, query_shape)
    v = pad_to_multiple_nd(v, query_shape)

    # extract query and memory blocks
    if decode_step is None:
      q = break_into_blocks_nd(q, query_shape)
    else:
      # in fast decoding, q has 1 block with 1 item in it
      # q shape will be [batch] + [1] * n + [1, depth] which is equivalent of
      # [batch, b1, b2, ..., bn, items_in_block, depth] where there is 1 block
      # and 1 item in that block
      q = tf.reshape(q, [-1] + [1] * (len(q_shape) - 3) + [q_shape[-1]])
    k = break_into_memory_blocks_nd(k, query_shape, memory_flange, masked=True)
    v = break_into_memory_blocks_nd(v, query_shape, memory_flange, masked=True)

    # extract just one block of k and v in fast decoding mode.
    if decode_step is not None:
      k = select_block_for_decode_step(k, decode_step, query_shape)
      v = select_block_for_decode_step(v, decode_step, query_shape)

    # flatten q, k and v to [batch, num_blocks, items_in_block, depth]
    q, blocks_per_dim = flatten_blocks_nd(q)
    k, _ = flatten_blocks_nd(k)
    v, _ = flatten_blocks_nd(v)

    # make attention bias for causal attention.
    causal_attn_bias = causal_attention_bias_nd(
        query_shape, memory_flange, decode_step=decode_step)
    padding_attn_bias = tf.expand_dims(
        embedding_to_padding(v[:1, :, :, :]) * -1e9, axis=-2)

    if decode_step is None:
      num_blocks = common_layers.shape_list(v)[1]
      causal_attn_bias = tf.tile(causal_attn_bias, [1, num_blocks, 1, 1])
      padding_attn_bias = tf.tile(
          padding_attn_bias,
          [1, 1, np.prod(query_shape, dtype=np.int32), 1])
    attn_bias = tf.minimum(causal_attn_bias, padding_attn_bias)

    # Calculate dot product attention
    output = dot_product_attention(
        q,
        k,
        v,
        attn_bias,
        dropout_rate=0.,
        name=name or "masked_local_nd",
        make_image_summary=False)

    # restructure the output from blocks ordering to the original ordering
    output = unflatten_blocks_nd(output, blocks_per_dim)
    if decode_step is None:
      # In fast decoding, output only contains one element, this is not needed.
      output = put_back_blocks_nd(output, query_shape)

    # bring back the heads dimension
    output_shape = common_layers.shape_list(output)
    output = tf.reshape(output, q_shape[:2] + output_shape[1:])
    if decode_step is None:
      # No padding is introduced in fast decoding, no need to do this.
      output_shape = common_layers.shape_list(output)
      output = tf.slice(output, [0] * len(output_shape),
                        [-1, -1] + q_shape[2:-1] + [-1])
    return output


def select_block_for_decode_step(blocked_x, decode_step, query_shape):
  """Selects one block from `x` that contains position `decode_step`.

  NOTE: This method only works for blocked inputs. It selects one block around
  `decode_step` position in blocked raster scan order.

  Args:
    blocked_x: a [batch, blocks_per_d1, ..., blocks_per_dn, b1 * ...* bn, depth]
      tensor
    decode_step: an integer
    query_shape: a tuple (q1, q2, ..., qn) representing query shape

  Returns:
     a [batch, [1] * n, b1 * ... * bn, depth] tensor
  """
  blocked_x_shape = common_layers.shape_list(blocked_x)
  # calculate the shape of the normal x
  x_shape = [b * q for b, q in zip(blocked_x_shape[1:-2], query_shape)]
  # Get the position of `decode_step` element in the unblocked x.
  index = decode_step_to_index(decode_step, query_shape, x_shape)
  # Convert it to the blocked positions.
  blocked_index = [i // q for i, q in zip(index, query_shape)]
  # TPU needs size to be non negative for the case when begin is not
  # compile-time constants.
  return tf.slice(blocked_x, [0] + blocked_index + [0, 0],
                  [blocked_x_shape[0]] + [1] * len(blocked_index) +
                  blocked_x_shape[-2:])


def flatten_blocks_nd(x):
  """Flattens blocks of the input tensor.

  Args:
    x: a [batch, b1, ..., bn, items_in_block, depth] tensor

  Returns:
    a flattened tensor of shape [batch, b1 * ...* bm, items_in_block, depth]
    a list of [b1, ..., bn] which is used for unflattening.
  """
  x_shape = common_layers.shape_list(x)
  num_blocks = np.prod(x_shape[1:-2], dtype=np.int32)
  return tf.reshape(x, [-1, num_blocks] + x_shape[-2:]), x_shape[1:-2]


def unflatten_blocks_nd(x, blocks_per_dimension):
  """Converts a flattened tensor into a normal blocked tensor.

  Args:
    x: a [batch, d1 * ... dn, items_in_block, depth] tensor
    blocks_per_dimension: a n-d list of integers for number of blocks in each
      dimension.

  Returns:
    a [batch, d1, d2, ..., dn, items_in_block, depth] tensor
  """
  x_shape = common_layers.shape_list(x)
  assert x_shape[1] == np.prod(blocks_per_dimension, dtype=np.int32)
  return tf.reshape(x, [-1] + list(blocks_per_dimension) + x_shape[-2:])


def break_into_memory_blocks_nd(x, query_shape, memory_flange, masked=False):
  """Break a tensor into memory blocks around query blocks.

  This requires memory_flange to be divisible by query_shape in every dimension.

  Args:
    x: a [batch, d1, d2, ..., dn, depth] tensor
    query_shape: a n-d list of integers representing query shape
    memory_flange: an n-d list of integers representing memory flange.
    masked: a boolean for masked vs unmasked attention.

  Returns:
    a [batch, blocks_per_d1, ..., blocks_per_dn, b1 * ...* bn, depth] where bi
      is the memory block size in dimension i which is equal to q[i] + 2m[i] or
      q[i] + m[i] if masked attention and i = 1.
  """
  assert all([m % b == 0 for b, m in zip(query_shape, memory_flange)])

  original_x_shape = common_layers.shape_list(x)
  # calculate the total number of query blocks in each dimension
  blocks_in_memory_flange = [m // b for b, m in zip(query_shape, memory_flange)]
  num_query_blocks = [
      l // q for l, q in zip(original_x_shape[1:-1], query_shape)
  ]
  # pad x to have enough items on the corners to form the  memory blocks.
  if masked:
    # Only pad the beginning of first dimension in masked mode.
    x = tf.pad(x, [[0, 0], [memory_flange[0], 0]] +
               [[p, p] for p in memory_flange[1:]] + [[0, 0]])
  else:
    x = tf.pad(x, [[0, 0]] + [[p, p] for p in memory_flange] + [[0, 0]])

  query_blocks = break_into_blocks_nd(x, query_shape)
  # stitch query blocks together to form memory blocks of the desired size.
  start_indices_per_dimension = []
  for dimension, blocks in enumerate(blocks_in_memory_flange):
    if masked and dimension == 0:
      # num blocks for first dimension in masked mode is blocks + 1
      size = blocks + 1
    else:
      size = 2 * blocks + 1
    start_indices_per_dimension.append(range(size))

  slices = []
  for start_indices in itertools.product(*start_indices_per_dimension):
    start = [0] + list(start_indices) + [0, 0]
    size = [-1] + num_query_blocks + [-1, -1]
    s = tf.slice(query_blocks, start, size)
    slices.append(s)
  # concat slices in their query block dimension to form the full memory blocks
  return tf.concat(slices, axis=-2)


def break_into_blocks_nd(x, block_shape):
  """Break input tensor into blocks of `block_shape`.

  Args:
    x: a [batch, d1, d2, ..., dn, depth] tensor
    block_shape: a n-d list of integers representing block shape

  Returns:
    a [batch, d1//block1, ..., dn//blockn, block1 *... * blockn, depth] tensor
  """
  x_shape = common_layers.shape_list(x)
  assert all([l % b == 0 for l, b in zip(x_shape[1:], block_shape)])
  blocks_per_dimension = [l // b for l, b in zip(x_shape[1:], block_shape)]
  # reshape to [-1, d1 // block1, block1, ..., dn // blockn, blockn, depth]
  reshape_to = list(
      itertools.chain.from_iterable(zip(blocks_per_dimension, block_shape)))
  x = tf.reshape(x, [-1] + reshape_to + x_shape[-1:])
  # transpose dimensions to bring the n-d blocks in consecutive dimensions.
  block_dimensions_index = [2 * (i + 1) for i in range(len(block_shape))]
  x = tf.transpose(x, [0] + [i - 1 for i in block_dimensions_index] +
                   block_dimensions_index + [2 * len(block_shape) + 1])
  return tf.reshape(x, [-1] + blocks_per_dimension +
                    [np.prod(block_shape, dtype=np.int32)] + x_shape[-1:])


def put_back_blocks_nd(x, block_shape):
  """Restructure input tensor from blocks to normal ordering.

  Args:
    x: a [batch, b1, ..., bn, items_in_block, depth] tensor
    block_shape: a n-d list of integers representing block shape.

  Returns:
    a [batch, d1, ..., dn, depth] where blocks are put back to form the
      original tensor.
  """
  x_shape = common_layers.shape_list(x)
  assert x_shape[-2] == np.prod(block_shape)
  x = tf.reshape(x, x_shape[:-2] + list(block_shape) + x_shape[-1:])
  block_dimension_index = [i + 1 for i in range(len(block_shape))]
  block_shape_index = [b + len(block_shape) for b in block_dimension_index]
  interleaved_dimensions = list(
      itertools.chain.from_iterable(
          zip(block_dimension_index, block_shape_index)))
  x = tf.transpose(x, [0] + interleaved_dimensions + [2 * len(block_shape) + 1])
  x_shape = common_layers.shape_list(x)
  x = tf.reshape(x, [-1] + [
      x_shape[2 * i + 1] * x_shape[2 * i + 2] for i in range(len(block_shape))
  ] + x_shape[-1:])
  return x


def pad_to_multiple_nd(x, block_shape):
  """Making sure x is a multiple of shape.

  Args:
    x: a [batch, d1, d2, ..., dn, depth] tensor
    block_shape: a n-d list of integers representing block shape

  Returns:
    padded x where each dimension is a multiple of corresponding block length.
  """
  shape = common_layers.shape_list(x)
  paddings = [-l % b for l, b in zip(shape[1:-1], block_shape)]
  return tf.pad(x, [[0, 0]] + [[0, p] for p in paddings] + [[0, 0]])


def causal_attention_bias_nd(query_shape, memory_flange, decode_step=None):
  """Creates causal attention bias for local nd attention.

  This assumes memory_flange is divisible by query_shape in every dimension.

  Args:
    query_shape: a n-d list of integers representing query shape
    memory_flange: a n-d list of integers representing memory flange
    decode_step: an integer

  Returns:
    a [1, 1, query_items, memory_items] tensor for masked attention bias or
    a [1, 1, 1, memory_items] tensor if decode_step is not None.
  """
  assert all([m % q == 0 for q, m in zip(query_shape, memory_flange)])
  blocks_per_memory_flange = [
      m // q for q, m in zip(query_shape, memory_flange)
  ]
  # previous blocks will be half the number of all blocks if we select blocks
  # to the left and right of center block in every dimension.
  prev_blocks = np.prod([2 * b + 1 for b in blocks_per_memory_flange],
                        dtype=np.int32) // 2
  all_blocks = np.prod(
      [blocks_per_memory_flange[0] + 1] +
      [2 * b + 1 for b in blocks_per_memory_flange[1:]],
      dtype=np.int32)
  future_blocks = all_blocks - prev_blocks - 1
  # add unmasked biases for all prev blocks and a lower triangle for the center
  # block and all masked for future blocks.
  items_in_block = np.prod(query_shape, dtype=np.int32)
  items_in_query = items_in_block if decode_step is None else 1
  prev_blocks_attn = tf.zeros(
      [1, 1, items_in_query, prev_blocks * items_in_block])

  # add mask for the center block
  if decode_step is None:
    center_block_attn = attention_bias_lower_triangle(items_in_block)
  else:
    step_in_block = decode_step % items_in_block
    cond = tf.reshape(
        tf.less_equal(tf.range(items_in_block, dtype=tf.int32), step_in_block),
        [1, 1, items_in_query, items_in_block])
    center_block_attn = tf.where(
        cond, tf.zeros([1, 1, items_in_query, items_in_block]),
        -1e9 * tf.ones([1, 1, items_in_query, items_in_block]))

  # add mask for all future blocks
  future_blocks_attn = -1e9 * tf.ones(
      [1, 1, items_in_query, future_blocks * items_in_block])
  return tf.concat([prev_blocks_attn, center_block_attn, future_blocks_attn],
                   axis=3)


def compute_attention_component(antecedent,
                                total_depth,
                                filter_width=1,
                                padding="VALID",
                                name="c",
                                vars_3d_num_heads=0,
                                layer_collection=None):
  """Computes attention component (query, key or value).

  Args:
    antecedent: a Tensor with shape [batch, length, channels]
    total_depth: an integer
    filter_width: An integer specifying how wide you want the attention
      component to be.
    padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
    name: a string specifying scope name.
    vars_3d_num_heads: an optional integer (if we want to use 3d variables)
    layer_collection: A tensorflow_kfac.LayerCollection. Only used by the
      KFAC optimizer. Default is None.

  Returns:
    c : [batch, length, depth] tensor
  """
  if layer_collection is not None:
    if filter_width != 1 or vars_3d_num_heads != 0:
      raise ValueError(
          "KFAC implementation only supports filter_width=1 (actual: {}) and "
          "vars_3d_num_heads=0 (actual: {}).".format(
              filter_width, vars_3d_num_heads))
  if vars_3d_num_heads is not None and vars_3d_num_heads > 0:
    assert filter_width == 1
    input_depth = antecedent.get_shape().as_list()[-1]
    depth_per_head = total_depth // vars_3d_num_heads
    initializer_stddev = input_depth ** -0.5
    if "q" in name:
      initializer_stddev *= depth_per_head ** -0.5
    var = tf.get_variable(
        name, [input_depth,
               vars_3d_num_heads,
               total_depth // vars_3d_num_heads],
        initializer=tf.random_normal_initializer(stddev=initializer_stddev))
    var = tf.cast(var, antecedent.dtype)
    var = tf.reshape(var, [input_depth, total_depth])
    return tf.tensordot(antecedent, var, axes=1)
  if filter_width == 1:
    return common_layers.dense(
        antecedent, total_depth, use_bias=False, name=name,
        layer_collection=layer_collection)
  else:
    return common_layers.conv1d(
        antecedent, total_depth, filter_width, padding=padding, name=name)


def compute_qkv(query_antecedent,
                memory_antecedent,
                total_key_depth,
                total_value_depth,
                q_filter_width=1,
                kv_filter_width=1,
                q_padding="VALID",
                kv_padding="VALID",
                vars_3d_num_heads=0,
                layer_collection=None):
  """Computes query, key and value.

  Args:
    query_antecedent: a Tensor with shape [batch, length_q, channels]
    memory_antecedent: a Tensor with shape [batch, length_m, channels]
    total_key_depth: an integer
    total_value_depth: an integer
    q_filter_width: An integer specifying how wide you want the query to be.
    kv_filter_width: An integer specifying how wide you want the keys and values
    to be.
    q_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
    kv_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
    vars_3d_num_heads: an optional (if we want to use 3d variables)
    layer_collection: A tensorflow_kfac.LayerCollection. Only used by the
      KFAC optimizer. Default is None.

  Returns:
    q, k, v : [batch, length, depth] tensors
  """
  if memory_antecedent is None:
    memory_antecedent = query_antecedent
  q = compute_attention_component(
      query_antecedent,
      total_key_depth,
      q_filter_width,
      q_padding,
      "q",
      vars_3d_num_heads=vars_3d_num_heads,
      layer_collection=layer_collection)
  k = compute_attention_component(
      memory_antecedent,
      total_key_depth,
      kv_filter_width,
      kv_padding,
      "k",
      vars_3d_num_heads=vars_3d_num_heads,
      layer_collection=layer_collection)
  v = compute_attention_component(
      memory_antecedent,
      total_value_depth,
      kv_filter_width,
      kv_padding,
      "v",
      vars_3d_num_heads=vars_3d_num_heads,
      layer_collection=layer_collection)
  return q, k, v


def multihead_attention(query_antecedent,
                        memory_antecedent,
                        bias,
                        total_key_depth,
                        total_value_depth,
                        output_depth,
                        num_heads,
                        dropout_rate,
                        attention_type="dot_product",
                        max_relative_position=None,
                        heads_share_relative_embedding=False,
                        add_relative_to_values=False,
                        image_shapes=None,
                        block_length=128,
                        block_width=128,
                        q_filter_width=1,
                        kv_filter_width=1,
                        q_padding="VALID",
                        kv_padding="VALID",
                        cache=None,
                        gap_size=0,
                        num_memory_blocks=2,
                        name="multihead_attention",
                        save_weights_to=None,
                        make_image_summary=True,
                        dropout_broadcast_dims=None,
                        vars_3d=False,
                        layer_collection=None,
                        recurrent_memory=None,
                        chunk_number=None,
                        hard_attention_k=0,
                        gumbel_noise_weight=0.0,
                        max_area_width=1,
                        max_area_height=1,
                        memory_height=1,
                        area_key_mode="mean",
                        area_value_mode="sum",
                        training=True,
                        **kwargs):
  """Multihead scaled-dot-product attention with input/output transformations.

  Args:
    query_antecedent: a Tensor with shape [batch, length_q, channels]
    memory_antecedent: a Tensor with shape [batch, length_m, channels] or None
    bias: bias Tensor (see attention_bias())
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    dropout_rate: a floating point number
    attention_type: a string, either "dot_product", "dot_product_relative",
                    "local_mask_right", "local_unmasked", "masked_dilated_1d",
                    "unmasked_dilated_1d", graph, or any attention function
                    with the signature (query, key, value, **kwargs)
    max_relative_position: Maximum distance between inputs to generate
                           unique relation embeddings for. Only relevant
                           when using "dot_product_relative" attention.
    heads_share_relative_embedding: boolean to share relative embeddings
    add_relative_to_values: a boolean for whether to add relative component to
                            values.
    image_shapes: optional tuple of integer scalars.
                  see comments for attention_image_summary()
    block_length: an integer - relevant for "local_mask_right"
    block_width: an integer - relevant for "local_unmasked"
    q_filter_width: An integer specifying how wide you want the query to be.
    kv_filter_width: An integer specifying how wide you want the keys and values
                     to be.
    q_padding: One of "VALID", "SAME" or "LEFT". Default is VALID: No padding.
               kv_padding: One of "VALID", "SAME" or "LEFT". Default is "VALID":
               no padding.
    cache: dict containing Tensors which are the results of previous
           attentions, used for fast decoding. Expects the dict to contrain two
           keys ('k' and 'v'), for the initial call the values for these keys
           should be empty Tensors of the appropriate shape.
               'k' [batch_size, 0, key_channels]
               'v' [batch_size, 0, value_channels]
    gap_size: Integer option for dilated attention to indicate spacing between
              memory blocks.
    num_memory_blocks: Integer option to indicate how many memory blocks to look
                       at.
    name: an optional string.
    save_weights_to: an optional dictionary to capture attention weights
      for vizualization; the weights tensor will be appended there under
      a string key created from the variable scope (including name).
    make_image_summary: Whether to make an attention image summary.
    dropout_broadcast_dims:  an optional list of integers less than 4
      specifying in which dimensions to broadcast the dropout decisions.
      saves memory.
    vars_3d: use 3-dimensional variables for input/output transformations
    layer_collection: A tensorflow_kfac.LayerCollection. Only used by the
      KFAC optimizer. Default is None.
    recurrent_memory: An optional transformer_memory.RecurrentMemory, which
      retains state across chunks. Default is None.
    chunk_number: an optional integer Tensor with shape [batch] used to operate
      the recurrent_memory.
    hard_attention_k: integer, if > 0 triggers hard attention (picking top-k).
    gumbel_noise_weight: if > 0, apply Gumbel noise with weight
      `gumbel_noise_weight` before picking top-k. This is a no op if
      hard_attention_k <= 0.
    max_area_width: the max width allowed for an area.
    max_area_height: the max height allowed for an area.
    memory_height: the height of the memory.
    area_key_mode: the mode for computing area keys, which can be "mean",
      "concat", "sum", "sample_concat", and "sample_sum".
    area_value_mode: the mode for computing area values, which can be either
      "mean", or "sum".
    training: indicating if it is in the training mode.
    **kwargs (dict): Parameters for the attention function.

  Caching:
    WARNING: For decoder self-attention, i.e. when memory_antecedent == None,
    the caching assumes that the bias contains future masking.

    The caching works by saving all the previous key and value values so that
    you are able to send just the last query location to this attention
    function. I.e. if the cache dict is provided it assumes the query is of the
    shape [batch_size, 1, hidden_dim] rather than the full memory.

  Returns:
    The result of the attention transformation. The output shape is
        [batch_size, length_q, hidden_dim]
    unless the cache dict is provided in which case only the last memory
    position is calculated and the output shape is [batch_size, 1, hidden_dim]
    Optionally returns an additional loss parameters (ex: load balance loss for
    the experts) returned by the attention_type function.

  Raises:
    ValueError: if the key depth or value depth are not divisible by the
      number of attention heads.
  """
  if total_key_depth % num_heads != 0:
    raise ValueError("Key depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_key_depth, num_heads))
  if total_value_depth % num_heads != 0:
    raise ValueError("Value depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_value_depth, num_heads))
  vars_3d_num_heads = num_heads if vars_3d else 0

  if layer_collection is not None:
    if cache is not None:
      raise ValueError("KFAC implementation only supports cache is None.")
    if vars_3d:
      raise ValueError("KFAC implementation does not support 3d vars.")

  if recurrent_memory is not None:
    if memory_antecedent is not None:
      raise ValueError("Recurrent memory requires memory_antecedent is None.")
    if cache is not None:
      raise ValueError("Cache is not supported when using recurrent memory.")
    if vars_3d:
      raise ValueError("3d vars are not supported when using recurrent memory.")
    if layer_collection is not None:
      raise ValueError("KFAC is not supported when using recurrent memory.")
    if chunk_number is None:
      raise ValueError("chunk_number is required when using recurrent memory.")

  with tf.variable_scope(name, default_name="multihead_attention",
                         values=[query_antecedent, memory_antecedent]):

    if recurrent_memory is not None:
      (
          recurrent_memory_transaction,
          query_antecedent, memory_antecedent, bias,
      ) = recurrent_memory.pre_attention(
          chunk_number,
          query_antecedent, memory_antecedent, bias,
      )

    if cache is None or memory_antecedent is None:
      q, k, v = compute_qkv(query_antecedent, memory_antecedent,
                            total_key_depth, total_value_depth, q_filter_width,
                            kv_filter_width, q_padding, kv_padding,
                            vars_3d_num_heads=vars_3d_num_heads,
                            layer_collection=layer_collection)
    if cache is not None:
      if attention_type not in ["dot_product", "dot_product_relative"]:
        # TODO(petershaw): Support caching when using relative position
        # representations, i.e. "dot_product_relative" attention.
        raise NotImplementedError(
            "Caching is not guaranteed to work with attention types other than"
            " dot_product.")
      if bias is None:
        raise ValueError("Bias required for caching. See function docstring "
                         "for details.")

      if memory_antecedent is not None:
        # Encoder-Decoder Attention Cache
        q = compute_attention_component(query_antecedent, total_key_depth,
                                        q_filter_width, q_padding, "q",
                                        vars_3d_num_heads=vars_3d_num_heads)
        k = cache["k_encdec"]
        v = cache["v_encdec"]
      else:
        k = split_heads(k, num_heads)
        v = split_heads(v, num_heads)
        decode_loop_step = kwargs.get("decode_loop_step")
        if decode_loop_step is None:
          k = cache["k"] = tf.concat([cache["k"], k], axis=2)
          v = cache["v"] = tf.concat([cache["v"], v], axis=2)
        else:
          tmp_k = tf.transpose(cache["k"], perm=[2, 0, 1, 3])
          tmp_k = tf.add(tmp_k, tf.scatter_nd([[decode_loop_step]], tf.expand_dims(tf.squeeze(k, axis=2), 0), tmp_k.shape))
          k = cache["k"] = tf.transpose(tmp_k, perm=[1, 2, 0, 3])

          tmp_v = tf.transpose(cache["v"], perm=[2, 0, 1, 3])
          tmp_v = tf.add(tmp_v, tf.scatter_nd([[decode_loop_step]], tf.expand_dims(tf.squeeze(v, axis=2), 0), tmp_v.shape))
          v = cache["v"] = tf.transpose(tmp_v, perm=[1, 2, 0, 3])

    q = split_heads(q, num_heads)
    if cache is None:
      k = split_heads(k, num_heads)
      v = split_heads(v, num_heads)

    key_depth_per_head = total_key_depth // num_heads
    if not vars_3d:
      q *= key_depth_per_head**-0.5

    additional_returned_value = None
    if callable(attention_type):  # Generic way to extend multihead_attention
      x = attention_type(q, k, v, **kwargs)
      if isinstance(x, tuple):
        x, additional_returned_value = x  # Unpack
    elif attention_type == "dot_product":
      if max_area_width > 1 or max_area_height > 1:
        x = area_attention.dot_product_area_attention(
            q, k, v, bias, dropout_rate, image_shapes,
            save_weights_to=save_weights_to,
            dropout_broadcast_dims=dropout_broadcast_dims,
            max_area_width=max_area_width,
            max_area_height=max_area_height,
            memory_height=memory_height,
            area_key_mode=area_key_mode,
            area_value_mode=area_value_mode,
            training=training)
      else:
        x = dot_product_attention(
            q, k, v, bias, dropout_rate, image_shapes,
            save_weights_to=save_weights_to,
            make_image_summary=make_image_summary,
            dropout_broadcast_dims=dropout_broadcast_dims,
            activation_dtype=kwargs.get("activation_dtype"),
            hard_attention_k=hard_attention_k,
            gumbel_noise_weight=gumbel_noise_weight)
    elif attention_type == "dot_product_relative":
      x = dot_product_attention_relative(
          q,
          k,
          v,
          bias,
          max_relative_position,
          dropout_rate,
          image_shapes,
          save_weights_to=save_weights_to,
          make_image_summary=make_image_summary,
          cache=cache is not None,
          allow_memory=recurrent_memory is not None,
          hard_attention_k=hard_attention_k,
          gumbel_noise_weight=gumbel_noise_weight)
    elif attention_type == "dot_product_unmasked_relative_v2":
      x = dot_product_unmasked_self_attention_relative_v2(
          q,
          k,
          v,
          bias,
          max_relative_position,
          dropout_rate,
          image_shapes,
          save_weights_to=save_weights_to,
          make_image_summary=make_image_summary,
          dropout_broadcast_dims=dropout_broadcast_dims,
          heads_share_relative_embedding=heads_share_relative_embedding,
          add_relative_to_values=add_relative_to_values)
    elif attention_type == "dot_product_relative_v2":
      x = dot_product_self_attention_relative_v2(
          q,
          k,
          v,
          bias,
          max_relative_position,
          dropout_rate,
          image_shapes,
          save_weights_to=save_weights_to,
          make_image_summary=make_image_summary,
          dropout_broadcast_dims=dropout_broadcast_dims,
          heads_share_relative_embedding=heads_share_relative_embedding,
          add_relative_to_values=add_relative_to_values)
    elif attention_type == "local_within_block_mask_right":
      x = masked_within_block_local_attention_1d(
          q, k, v, block_length=block_length)
    elif attention_type == "local_relative_mask_right":
      x = masked_relative_local_attention_1d(
          q,
          k,
          v,
          block_length=block_length,
          make_image_summary=make_image_summary,
          dropout_rate=dropout_rate,
          heads_share_relative_embedding=heads_share_relative_embedding,
          add_relative_to_values=add_relative_to_values,
          name="masked_relative_local_attention_1d")
    elif attention_type == "local_mask_right":
      x = masked_local_attention_1d(
          q,
          k,
          v,
          block_length=block_length,
          make_image_summary=make_image_summary)
    elif attention_type == "local_unmasked":
      x = local_attention_1d(
          q, k, v, block_length=block_length, filter_width=block_width)
    elif attention_type == "masked_dilated_1d":
      x = masked_dilated_self_attention_1d(q, k, v, block_length, block_width,
                                           gap_size, num_memory_blocks)
    else:
      assert attention_type == "unmasked_dilated_1d"
      x = dilated_self_attention_1d(q, k, v, block_length, block_width,
                                    gap_size, num_memory_blocks)
    x = combine_heads(x)

    # Set last dim specifically.
    x.set_shape(x.shape.as_list()[:-1] + [total_value_depth])

    if vars_3d:
      o_var = tf.get_variable(
          "o", [num_heads, total_value_depth // num_heads, output_depth])
      o_var = tf.cast(o_var, x.dtype)
      o_var = tf.reshape(o_var, [total_value_depth, output_depth])
      x = tf.tensordot(x, o_var, axes=1)
    else:
      x = common_layers.dense(
          x, output_depth, use_bias=False, name="output_transform",
          layer_collection=layer_collection)

    if recurrent_memory is not None:
      x = recurrent_memory.post_attention(recurrent_memory_transaction, x)
    if additional_returned_value is not None:
      return x, additional_returned_value
    return x


def multihead_attention_2d(query_antecedent,
                           memory_antecedent,
                           total_key_depth,
                           total_value_depth,
                           output_depth,
                           num_heads,
                           attention_type="local_attention_2d",
                           query_shape=(8, 16),
                           memory_flange=(8, 16),
                           name=None):
  """2d Multihead scaled-dot-product attention with inp/output transformations.

  Args:
    query_antecedent: a Tensor with shape [batch, h, w, depth_k]
    memory_antecedent: a Tensor with shape [batch, h, w, depth_k]
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    attention_type: String, type of attention function to use.
    query_shape: an tuple indicating the height and width of each query block.
    memory_flange: an integer indicating how much to look in height and width
    name: an optional string

  Returns:
    A Tensor of shape [batch, h, w, output_depth]

  Raises:
    ValueError: if the key depth or value depth are not divisible by the
      number of attention heads.
  """
  if total_key_depth % num_heads != 0:
    raise ValueError("Key depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_key_depth, num_heads))
  if total_value_depth % num_heads != 0:
    raise ValueError("Value depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_value_depth, num_heads))
  with tf.variable_scope(
      name,
      default_name="multihead_attention_2d",
      values=[query_antecedent, memory_antecedent]):
    q, k, v = compute_qkv(query_antecedent, memory_antecedent, total_key_depth,
                          total_value_depth)
    # after splitting, shape is [batch, heads, h, w, depth]
    q = split_heads_2d(q, num_heads)
    k = split_heads_2d(k, num_heads)
    v = split_heads_2d(v, num_heads)
    key_depth_per_head = total_key_depth // num_heads
    q *= key_depth_per_head**-0.5
    if attention_type == "local_attention_2d":
      x = local_attention_2d(
          q, k, v, query_shape=query_shape, memory_flange=memory_flange)
    elif attention_type == "masked_local_attention_2d":
      assert attention_type == "masked_local_attention_2d"
      x = masked_local_attention_2d(
          q, k, v, query_shape=query_shape, memory_flange=memory_flange)
    else:
      assert attention_type == "unmasked_local_attention_2d_tpu"
      x = dot_product_unmasked_attention_local_2d_tpu(
          q, k, v, None, max_relative_position=None, query_shape=query_shape)
    x = combine_heads_2d(x)
    x = common_layers.dense(
        x, output_depth, use_bias=False, name="output_transform")
    return x


def multihead_attention_nd(query_antecedent,
                           memory_antecedent,
                           total_key_depth,
                           total_value_depth,
                           output_depth,
                           num_heads,
                           query_shape,
                           memory_flange,
                           masked=False,
                           cache=None,
                           decode_step=None,
                           name=None):
  """n-d Multihead scaled-dot-product attention with in/output transformations.

  Args:
    query_antecedent: a Tensor with shape [batch, d1, ..., dn, depth_q] or
      [batch, 1, ..., 1, depth_q] if in fast decoding mode.
    memory_antecedent: a Tensor with shape [batch, d1, ..., dn, depth_m] or None
      for self attention.
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    query_shape: an tuple indicating the dimensions of each query block.
    memory_flange: an integer indicating how much to look around a query block
      in each dimension
    masked: a boolean to specify whether to do masked or unmasked attention.
    cache: a dict like: {
      'k': [batch, num_heads, d1, ..., dn, depth_k // num_heads],
      'v': [batch, num_heads, d1, ..., dn, depth_v // num_heads]} Caller should
        initially pass zero tensors for `decode_step` == 0. This method will
        update cache and caller should pass the same cache in consecutive calls.
        This works for both GPU and TPU inference. Caller should pass the latest
        query via `query_antecedent`. `memory_antecedent` should be None in this
        case, since auto-regressive decoding only applies to self attention.
    decode_step: integer to pass in decoding mode. `cache` and `decode_step`
      should both be set in decoding mode. Caller can also pass an empty `cache`
      without `decode_step`, for this method to initialize the cache for future
      calls with `decode_step` > 0.
    name: an optional string

  Returns:
    A Tensor of shape [batch, d1, ..., dn, output_depth] or
    [batch, 1, ..., 1, output_depth] if decode_step is set.

  Raises:
    ValueError: if the key depth or value depth are not divisible by the
      number of attention heads.
  """
  if total_key_depth % num_heads != 0:
    raise ValueError("Key depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_key_depth, num_heads))
  if total_value_depth % num_heads != 0:
    raise ValueError("Value depth (%d) must be divisible by the number of "
                     "attention heads (%d)." % (total_value_depth, num_heads))
  # Validate decoding input params are sensible.
  if decode_step is not None:
    assert "k" in cache and "v" in cache
  if cache is not None:
    assert memory_antecedent is None

  with tf.variable_scope(
      name,
      default_name="multihead_attention_nd",
      values=[query_antecedent, memory_antecedent]):
    if decode_step is not None:
      latest_antecedent = query_antecedent
      q, latest_k, latest_v = compute_qkv(latest_antecedent, None,
                                          total_key_depth, total_value_depth)
      latest_k = split_heads_nd(latest_k, num_heads)
      latest_v = split_heads_nd(latest_v, num_heads)
      # put latest k and v into their correct position in cache.
      k = cache["k"]
      v = cache["v"]
      k = put_item_in_decode_step(k, latest_k, decode_step, query_shape)
      v = put_item_in_decode_step(v, latest_v, decode_step, query_shape)
      cache["k"] = k
      cache["v"] = v

    else:
      q, k, v = compute_qkv(query_antecedent, memory_antecedent,
                            total_key_depth, total_value_depth)
      k = split_heads_nd(k, num_heads)
      v = split_heads_nd(v, num_heads)
      if cache is not None:
        cache["k"] = k
        cache["v"] = v
    # after splitting, shape is [batch, heads, d1, ..., dn, depth]
    q = split_heads_nd(q, num_heads)
    key_depth_per_head = total_key_depth // num_heads
    q *= key_depth_per_head**-0.5
    if masked:
      x = masked_local_attention_nd(
          q,
          k,
          v,
          query_shape=query_shape,
          memory_flange=memory_flange,
          decode_step=decode_step)
    else:
      raise NotImplementedError(
          "Unmaked multihead attention nd is not implemented")
    x = combine_heads_nd(x)
    x = common_layers.dense(
        x, output_depth, use_bias=False, name="output_transform")
    return x


def decode_step_to_index(decode_step, query_shape, tensor_shape):
  """Maps decode step to n-d index according to blocked raster scan order.

  Args:
    decode_step: an integer
    query_shape: a tuple (q1, q2, ..., qn) representing the query shape
    tensor_shape: a tuple (d1, d2, ..., dn) representing the tensor shape, minus
      the batch and depth dimensions.

  Returns:
    a tuple (i1, i2, ..., in) representing the index of the element at
    `decode_step` w.r.t. blocked raster scan order.
  """
  assert len(query_shape) == len(tensor_shape)
  blocks_per_dimension = [t // q for t, q in zip(tensor_shape, query_shape)]
  items_in_block = np.prod(query_shape, dtype=np.int32)
  step_block = decode_step // items_in_block
  step_within_block = decode_step % items_in_block

  block_index = []
  for q in blocks_per_dimension[::-1]:
    block_index.insert(0, step_block % q)
    step_block //= q

  within_block_index = []
  for q in query_shape[::-1]:
    within_block_index.insert(0, step_within_block % q)
    step_within_block //= q

  final_index = [
      w + b * q for w, b, q in zip(within_block_index, block_index, query_shape)
  ]
  return tuple(final_index)


def get_item_at_decode_step(x, decode_step, query_shape):
  """Extracts a single item from an n-d tensor at `decode_step` position.

  Args:
    x: a [batch, d1, d2, ..., dn, depth] tensor
    decode_step: an integer
    query_shape: a tuple (q1, q2, ..., qn) representing the query shape

  Returns:
    a [batch, 1, 1, ..., 1, depth] tensor that is a single element from `x` at
    `decode_step` w.r.t. blocked raster scan order.
  """
  x_shape = common_layers.shape_list(x)
  index = decode_step_to_index(decode_step, query_shape, x_shape[1:-1])
  # TPU needs size to be non negative for the case when begins are not
  # compile-time constants.
  return tf.slice(x, [0] + list(index) + [0],
                  [x_shape[0]] + [1] * len(index) + [x_shape[-1]])


def put_item_in_decode_step(x, item, decode_step, query_shape):
  """Puts a single item into an n-d tensor at `decode_step` position.

  Args:
    x: a [batch, heads, d1, d2, ..., dn, depth] tensor
    item: a [batch, heads, 1, 1, ..., 1, depth] tensor
    decode_step: an integer
    query_shape: a tuple (q1, q2, ..., qn) representing the query shape

  Returns:
    a [batch, heads, d1, d2, ..., dn, depth] tensor with value at `decode_step`
    w.r.t. blocked raster scan order is updated to be `item`.
  """
  x_shape = common_layers.shape_list(x)
  index = decode_step_to_index(decode_step, query_shape, x_shape[2:-1])
  # inplace_update only works on the first dimension, we need to flatten and
  # move batch to be the second dimension.
  flattened_x = tf.reshape(
      x, [-1, x_shape[1], np.prod(x_shape[2:-1]), x_shape[-1]])
  # transpose to [positions, batch, heads, depth]
  flattened_x = tf.transpose(flattened_x, [2, 0, 1, 3])

  flattened_index = 0
  factor = 1
  for d, idx in zip(x_shape[-2:1:-1], index[::-1]):
    flattened_index += idx * factor
    factor *= d

  item_shape = common_layers.shape_list(item)
  item = tf.reshape(item, item_shape[:2] + item_shape[-1:])
  updated_x = inplace_ops.alias_inplace_update(flattened_x, flattened_index,
                                               item)
  # unflatten the results
  updated_x = tf.transpose(updated_x, [1, 2, 0, 3])
  return tf.reshape(updated_x, [-1, x_shape[1]] + x_shape[2:])


def ffn_self_attention_layer(x,
                             filter_depth,
                             output_depth,
                             num_parts,
                             dropout_rate,
                             share_kv=False,
                             name=None):
  """Self-attention feedforward layer.

  We use self-attention to do feedforward computations. We apply this function
  positionwise where for each position, we linearly transform the output to have
  depth filter_depth, and break up the result depth-wise into num_parts
  contiguous parts. The parts self-attend, we concatenate the results
  depth-wise, and we linearly transform to a depth of output_depth. The goal is
  to get multiplicative interactions between components of a representation.

  Args:
    x: a Tensor with shape [batch, length, channels]
    filter_depth: an integer
    output_depth: an integer
    num_parts: an integer dividing filter depth
    dropout_rate: a floating point number
    share_kv: Share the key value transform
    name: an optional string

  Returns:
    A Tensor with shape [batch, length, output_depth].
  """
  with tf.variable_scope(
      name, default_name="feedforward_self_attention", values=[x]):
    x_shape = common_layers.shape_list(x)
    part_depth = filter_depth // num_parts
    if not share_kv:
      combined = common_layers.dense(
          x, filter_depth * 3, use_bias=False, name="qkv_transform")
      combined = tf.expand_dims(combined, axis=2)
      q, k, v = tf.split(combined, 3, axis=3)
    else:
      q = tf.expand_dims(
          common_layers.dense(
              x, filter_depth, use_bias=False, name="q_transform"),
          axis=2)
      kv_combined = tf.expand_dims(
          common_layers.dense(
              tf.concat([x, x], axis=1),
              filter_depth,
              use_bias=False,
              name="kv_transform"),
          axis=2)
      k, v = tf.split(kv_combined, [x_shape[1], x_shape[1]], axis=1)

    batch_q = tf.reshape(q, [-1, 1, num_parts, part_depth])
    batch_k = tf.reshape(k, [-1, 1, num_parts, part_depth])
    batch_v = tf.reshape(v, [-1, 1, num_parts, part_depth])

    batch_q *= part_depth**-0.5
    # non-masked bias
    bias = None
    x = dot_product_attention(batch_q, batch_k, batch_v, bias, dropout_rate)
    x = tf.reshape(x, [x_shape[0], x_shape[1], filter_depth])
    x = common_layers.dense(
        x, output_depth, use_bias=False, name="output_transform")
    return x


def parameter_attention(x,
                        total_key_depth,
                        total_value_depth,
                        output_depth,
                        memory_rows,
                        num_heads,
                        dropout_rate,
                        name=None):
  """Attention over parameters.

  We use the same multi-headed attention as in the other layers, but the memory
  keys and values are model parameters. There are no linear transformation on
  the keys or values.

  We are also a bit more careful about memory usage, since the number of
  memory positions may be very large.

  Args:
    x: a Tensor with shape [batch, length_q, channels]
    total_key_depth: an integer
    total_value_depth: an integer
    output_depth: an integer
    memory_rows: an integer
    num_heads: an integer dividing total_key_depth and total_value_depth
    dropout_rate: a floating point number
    name: an optional string

  Returns:
    A Tensor with shape [batch, length_q, output_depth].
  """
  with tf.variable_scope(name, default_name="parameter_attention", values=[x]):
    head_size_k = total_key_depth // num_heads
    head_size_v = total_value_depth // num_heads
    var_shape_k = [num_heads, memory_rows, head_size_k]
    var_shape_v = [num_heads, memory_rows, head_size_v]
    k = tf.get_variable(
        "k",
        var_shape_k,
        initializer=tf.random_normal_initializer(
            0, output_depth**-0.5 * (num_heads**0.5)))
    v = tf.get_variable(
        "v",
        var_shape_v,
        initializer=tf.random_normal_initializer(
            0, output_depth**-0.5 * (output_depth**0.5)))
    batch_size = common_layers.shape_list(x)[0]
    length = common_layers.shape_list(x)[1]
    q = common_layers.dense(
        x, total_key_depth, use_bias=False, name="q_transform")
    if dropout_rate:
      # This is a cheaper form of attention dropout where we use to use
      # the same dropout decisions across batch elements and query positions,
      # but different decisions across heads and memory positions.
      v = tf.nn.dropout(
          v, 1.0 - dropout_rate, noise_shape=[num_heads, memory_rows, 1])
    # query is [batch, length, hidden_size]
    # reshape and transpose it to [heads, batch * length, head_size]
    q = tf.reshape(q, [batch_size, length, num_heads, head_size_k])
    q = tf.transpose(q, [2, 0, 1, 3])
    q = tf.reshape(q, [num_heads, batch_size * length, head_size_k])
    weights = tf.matmul(q, k, transpose_b=True)
    weights = tf.nn.softmax(weights)
    y = tf.matmul(weights, v)
    y = tf.reshape(y, [num_heads, batch_size, length, head_size_v])
    y = tf.transpose(y, [1, 2, 0, 3])
    y = tf.reshape(y, [batch_size, length, total_value_depth])
    y.set_shape([None, None, total_value_depth])
    y = common_layers.dense(
        y, output_depth, use_bias=False, name="output_transform")
    return y


@expert_utils.add_name_scope()
def coordinate_tensor(shape, axis):
  """Return a tensor with given shape containing coordinate along given axis.

  Args:
    shape: a Tensor representing the shape of the output Tensor
    axis: an integer

  Returns:
    A tensor with shape shape and type tf.int32, where each elements its
    coordinate along the given axis.
  """
  if axis < 0:
    axis = tf.size(shape) + axis  # Convert to positive for the one_hot indice

  r = tf.range(shape[axis])
  r_shape = tf.one_hot(
      axis, tf.size(shape), on_value=-1, off_value=1, dtype=tf.int32)
  return tf.zeros(shape, dtype=tf.int32) + tf.reshape(r, r_shape)


def self_attention_expert(x,
                          batch_coordinate,
                          mask_right=True,
                          split_batch=False,
                          attention_num_head=1,
                          attention_kq_size=None,
                          attention_v_size=None):
  """Implementing attention that runs inside each expert.

  Args:
    x: A tensor of shape[batch, depth]. Contains representations from
      different positions, which are lexicographically ordered.
    batch_coordinate: A tensor of shape [batch, 1] containing the batch
      coordinate of each element in x. This is needed to make sure that
      positions from different sequences don't attend to each other.
    mask_right: A bool. If true, we will not attend to positions on the right,
      just as decoder self attention.
    split_batch (bool): If True, each sequence of the batch is processed
      individually on a loop. If False, the sequences are processed all at
      once and a mask is applied to isolate the sequences from each others
    attention_num_head (int): number of attention heads
    attention_kq_size (int): dimension used for the attention key, and query
    attention_v_size (int): dimension used for the attention value

  Returns:
    out: A tensor of shape [batch, depth].
  example use:
  expert_utils.local_moe(
     ...
     expert_fn=functools.partial(self_attention_expert, mask_right=)
     )
  """

  depth = x.get_shape().as_list()[-1]
  length = common_layers.shape_list(batch_coordinate)[0]

  # Print a warning message if one of the expert isn't used (useful at
  # inference where summaries aren't used and the gating function don't add
  # noise)
  global _expert_count  # Hack to make each expert have a unique id
  _expert_count += 1
  length = tf.cond(
      tf.equal(length, 0),
      lambda: tf.Print(  # pylint: disable=g-long-lambda
          length, [length], "Expert {} empty: ".format(_expert_count)),
      lambda: length,
  )

  tf.summary.scalar("batch_size", length, family="experts_stats_batch_size")

  attention_kq_size = attention_kq_size or depth
  attention_v_size = attention_v_size or depth

  def length_not_null(x, batch_coordinate):
    """Branch of the graph only evaluated when length isn't null."""

    # Mask between the sequences (not used if map_ids is used)
    bias_batch = attention_bias_coordinates(batch_coordinate)

    def add_or_set_if(prev_bias, new_bias, condition):
      """Add the bias together while considering the None case."""
      if not condition:
        return prev_bias
      if prev_bias is None:
        return new_bias
      return prev_bias + new_bias

    def mask_and_call_attention(x):
      """Function applied once for each sequence of the batch."""

      # Mask to prevent sequences of attending to the future
      length = common_layers.shape_list(x)[1]  # x has shape [1, length,...]
      bias_past = tf.reshape(
          attention_bias_lower_triangle(length), [length, length])
      # bias has shape [length, length]

      bias = None
      bias = add_or_set_if(bias, bias_past, mask_right)
      bias = add_or_set_if(bias, bias_batch, not split_batch)
      bias = tf.reshape(bias, [1, 1, length, length])

      return multihead_attention(
          x,
          None,
          bias,
          total_key_depth=attention_kq_size,
          total_value_depth=attention_v_size,
          output_depth=depth,
          num_heads=attention_num_head,
          dropout_rate=0.0)

    if split_batch:
      out = expert_utils.map_ids(x, batch_coordinate, mask_and_call_attention)
    else:
      x = tf.reshape(x, [1, length, depth])
      out = mask_and_call_attention(x)
      out = tf.squeeze(out, 0)
    return out

  # If the length is empty, just forward an empty tensor (avoid having to
  # evaluate multihead_attention with tensor having dim equal to zeros)
  out = tf.cond(
      tf.equal(length, 0),
      lambda: tf.zeros(shape=[0, depth], dtype=tf.float32, name="empty_out"),
      lambda: length_not_null(x, batch_coordinate),
  )
  return out


def local_expert_attention(x,
                           k,
                           loss_coef,
                           attention_num_experts,
                           train=True,
                           batch_coordinate=None,
                           **kwargs):
  """Attention using a mixture of experts.

    Positions sent to the same expert can attend to each other.
    The mixture of experts is "local" in that it is replicated on each
    datashard.

    local_moe flatten all batches so to avoid problems with padding (ex: all
    padding going to the same expert, self attention attending to non null
    padding tokens,...), the padding should be removed before.

  Args:
    x: a Tensor with shape [batch, length, depth] or [1, batch*length, depth]
    k: The number of experts to dispatch each example to
    loss_coef: a scalar. A multiplier for the expert loss
    attention_num_experts: The number of experts to use
    train: a boolean for the current mode
    batch_coordinate (tf.Tensor): int32 tensor of shape [1, batch*length, 1]
      containing the batch ids. If None, deduced from first dim of x.
    **kwargs: Arguments to forward to self_attention_expert

  Returns:
    y: a Tensor with shape [batch, length, depth]
    loss: a Scalar
  """
  if batch_coordinate is None:
    batch_coordinate = tf.expand_dims(
        coordinate_tensor(common_layers.shape_list(x)[:-1], axis=0), axis=-1)
  with tf.variable_scope("local_expert_attention"):
    additional_dispatch_params = {"batch_coordinate": batch_coordinate}
    return expert_utils.local_moe(
        x,
        train,
        functools.partial(self_attention_expert, **kwargs),
        attention_num_experts,
        k=k,
        loss_coef=loss_coef,
        pass_x=True,
        pass_gates=False,
        additional_dispatch_params=additional_dispatch_params,
    )


@expert_utils.add_name_scope()
def expert_dot_product(q, k, v, info_q, info_k):
  """Perform dot product on a subset of the sequence.

  Can add a mask to the attention to prevent sequences to attend to each other
  and to prevent attention to the future.

  Args:
    q (tf.Tensor): Queries of shape [length_expert_q, depth_k]
    k (tf.Tensor): Keys of shape [length_expert_k, depth_k]
    v (tf.Tensor): Values of shape [length_expert_k, depth_v]
    info_q (BatchInfo): Batch info for queries. If None, no mask is added
    info_k (BatchInfo): Batch info for keys

  Returns:
    tf.Tensor: dot product attention output ([length_expert_q, depth_v])
  """

  length_q = common_layers.shape_list(q)[0]
  length_k = common_layers.shape_list(k)[0]
  depth_v = v.get_shape().as_list()[-1]

  # Create the mask
  bias = attention_bias_coordinates(info_q.coordinates, info_k.coordinates)
  if info_k.order is not None:
    bias += attention_bias_future(info_q.order, info_k.order)

  # Restore batch and head dimension
  q, k, v = [tf.expand_dims(tf.expand_dims(t, 0), 0) for t in (q, k, v)]

  def is_zero():
    zeros = tf.zeros(shape=[1, 1, length_q, depth_v], dtype=tf.float32)
    zeros = tf.Print(zeros, [length_k, length_q], "length_k/length_q: ")
    return zeros

  def is_not_zero():
    return dot_product_attention(
        q,
        k,
        v,
        bias=bias,
        # No image summary to avoid "Retval[0] does not have value" (because
        # inside a condition)
        make_image_summary=False,
    )

  # TODO(epot): Should make sure a query gets at least one key. Because the
  # different sequences of a batch are merged, it's possible that a
  # query from a sequence only receive memory from another sequence, so
  # with the mask, the query will perform a softmax on -infinity values.
  # A hack could be to add at least one sequence of each batch on each group so
  # the query can attend to at least one element.
  # Softmax(Q.K)*V
  v_out = tf.cond(
      tf.logical_or(tf.equal(length_q, 0), tf.equal(length_k, 0)),
      is_zero,
      is_not_zero,
  )

  # Remove batch and head dimension
  v_out = tf.squeeze(v_out, axis=0)
  v_out = tf.squeeze(v_out, axis=0)
  return v_out


@expert_utils.add_name_scope()
def dot_product_single_head(q, k, v, gates_q, gates_k, bi):
  """Perform a dot product attention on a single sequence on a single head.

  This function dispatch the q, k, v and loop over the buckets to compute the
  attention dot product on each subsequences.

  Args:
    q (tf.Tensor): [length_q, depth_q]
    k (tf.Tensor): [length_k, depth_q]
    v (tf.Tensor): [length_k, depth_v]
    gates_q (tf.Tensor): One-hot vector of shape [length_q, nb_buckets]
    gates_k (tf.Tensor): One-hot vector of shape [length_k, nb_buckets]
    bi (BatchInfo): Contains the batch coordinates and sequence order

  Returns:
    tf.Tensor: [length_q, depth_v]
  """

  nb_buckets = gates_q.get_shape().as_list()[-1]

  q_dispatcher = expert_utils.SparseDispatcher(nb_buckets, gates_q)
  k_dispatcher = expert_utils.SparseDispatcher(nb_buckets, gates_k)

  def eventually_dispatch(dispatcher, value):
    if value is not None:
      return dispatcher.dispatch(value)
    return [None] * nb_buckets

  # Iterate over every dispatched group
  list_v_out = []
  for (
      q_i,
      k_i,
      v_i,
      qbc,
      qbo,
      kbc,
      kbo,
  ) in zip(
      # Dispatch queries, keys and values
      q_dispatcher.dispatch(q),
      k_dispatcher.dispatch(k),
      k_dispatcher.dispatch(v),
      # Also dispatch the sequence positions and batch coordinates
      eventually_dispatch(q_dispatcher, bi.coordinates),
      eventually_dispatch(q_dispatcher, bi.order),
      eventually_dispatch(k_dispatcher, bi.coordinates),
      eventually_dispatch(k_dispatcher, bi.order),
  ):
    list_v_out.append(
        expert_dot_product(
            q_i,
            k_i,
            v_i,
            info_q=BatchInfo(coordinates=qbc, order=qbo),
            info_k=BatchInfo(coordinates=kbc, order=kbo)))

  # Combine all buckets together to restore the original length
  return q_dispatcher.combine(list_v_out)


def map_fn_switch(fn, elems, use_map_fn=True, **kwargs):
  """Construct the graph with either tf.map_fn or a python for loop.

  This function is mainly for for benchmarking purpose.

  tf.map_fn is dynamic but is much slower than creating a static graph with
  for loop. However, having a for loop make the graph much longer to build
  and can consume too much RAM on distributed setting.

  Args:
    fn (fct): same that tf.map_fn but for now can only return a single tensor
      value (instead of a tuple of tensor for the general case)
    elems (tuple): same that tf.map_fn
    use_map_fn (bool): If True, tf.map_fn is used, if False, for _ in _: is used
      instead
    **kwargs: Additional tf.map_fn arguments (ignored if use_map_fn is False)

  Returns:
    tf.Tensor: the output of tf.map_fn
  """
  if use_map_fn:
    return tf.map_fn(fn, elems, **kwargs)
  elems_unpacked = (tf.unstack(e) for e in elems)
  out_unpacked = [fn(e) for e in zip(*elems_unpacked)]
  out = tf.stack(out_unpacked)
  return out


@expert_utils.add_name_scope()
def sparse_dot_product_attention(q, k, v, bi, use_map_fn, experts_params):
  """Sparse multihead self attention.

  Perform an approximation of the full multihead attention by dispatching
  the tokens using their keys/values. Thus the attention matrix are only
  computed each times on a subset of the tokens.

  Notes:
   * The function don't perform scaling here (multihead_attention does
  the /sqrt(depth)).
   * The padding should have been removed (so batch size should be 1 but length
   contains the elements from all different batches)
   * Right now, only self attention is supported so length_q and length_kv
   should be identical and the function will add triangular mask.
   * If bi.order is not None, The bias is added inside this function to
   prevent attention to the future.

  Args:
    q (tf.Tensor): Queries of shape [batch, heads, length_q, depth_k]
    k (tf.Tensor): Keys of shape [batch, heads, length_q, depth_k]
    v (tf.Tensor): Values of shape [batch, heads, length_kv, depth_v]
    bi (BatchInfo): Contains the batch coordinates and sequence order
    use_map_fn (bool): Use either tf.map_fn of python for loop to compute the
      heads separately
    experts_params (dict): Additional params for the local expert

  Returns:
    tf.Tensor: Approximation of Softmax(Q.K) * V, of shape
      [batch, heads, length_q, depth_v]
  """
  batch_size, nb_heads, _, depth = common_layers.shape_list(q)

  @expert_utils.add_name_scope()
  def flatten_first_dims(x):
    """Reshape such that x is [num_heads, -1, depth]."""
    # Case 1: Either constant batch size of size 1 or batch already flattened
    if x.get_shape().as_list()[0] == 1:
      return tf.squeeze(x, axis=0)

    # Case 2: Flatten batch dimension
    x = tf.transpose(x, perm=[1, 0, 2, 3])
    x = tf.reshape(x, [nb_heads, -1, depth])
    return x

  def flatten_batch(x):
    if x is None:
      return x
    return expert_utils.flatten_all_but_last(x)

  q = flatten_first_dims(q)
  k = flatten_first_dims(k)
  v = flatten_first_dims(v)
  bi = BatchInfo(
      coordinates=flatten_batch(bi.coordinates),
      order=flatten_batch(bi.order),
  )

  # Unstack heads
  list_q = tf.unstack(q)  # list[tf.Tensor(shape=[batch * length, depth])]
  list_k = tf.unstack(k)
  list_v = tf.unstack(v)

  list_gates_q = []
  list_gates_k = []

  total_loss = 0.0
  # There might be a more optimized way to compute all heads at once
  for single_q, single_k, _ in zip(list_q, list_k, list_v):
    # Each head get its own dispatcher
    lhs_gating = LshGating(
        depth=single_q.get_shape().as_list()[-1], **experts_params)

    list_gates_q.append(lhs_gating.get_gates(single_q))
    list_gates_k.append(lhs_gating.get_gates(single_k))

  gates_q = tf.stack(list_gates_q)
  gates_k = tf.stack(list_gates_k)

  # Process each head separately.
  v_out = map_fn_switch(
      lambda args: dot_product_single_head(bi=bi, *args),
      elems=(q, k, v, gates_q, gates_k),
      dtype=(tf.float32),
      parallel_iterations=2,
      use_map_fn=use_map_fn,
  )

  # Restore original shape as expected by multihead_attention
  if isinstance(batch_size, int) and batch_size == 1:
    v_out = tf.expand_dims(v_out, axis=0)  # Restore batch_size = 1
  else:
    v_out = tf.reshape(v_out, [nb_heads, batch_size, -1, depth])
    v_out = tf.transpose(v_out, [1, 0, 2, 3])
  return v_out, total_loss / nb_heads


@expert_utils.add_name_scope()
def dot_product_batched_head(q, k, v, gates_q, gates_k, mask_right=False):
  """Perform a dot product attention on a single sequence on a single head.

  This function dispatch the q, k, v and loop over the buckets to compute the
  attention dot product on each subsequences.

  Args:
    q (tf.Tensor): [batch*heads, length_q, depth_q]
    k (tf.Tensor): [batch*heads, length_k, depth_q]
    v (tf.Tensor): [batch*heads, length_k, depth_v]
    gates_q (tf.Tensor): One-hot of shape [batch*heads, length_q, nb_buckets]
    gates_k (tf.Tensor): One-hot of shape [batch*heads, length_k, nb_buckets]
    mask_right (bool): Add a bias to prevent attention to the future

  Returns:
    tf.Tensor: [length_q, depth_v]
  """
  nb_buckets = common_layers.shape_list(gates_q)[-1]

  @expert_utils.add_name_scope()
  def get_dispatcher(gates):
    """Construct dispatcher for gates."""
    length = common_layers.shape_list(gates)[1]
    # Count the number of ones per batch (and keep the max value)
    nb_elems_to_dispatch = tf.reduce_sum(gates, axis=[1, 2])
    nb_elems_to_dispatch = tf.reduce_max(nb_elems_to_dispatch)
    nb_elems_to_dispatch = tf.to_int32(nb_elems_to_dispatch)
    capacity = nb_elems_to_dispatch // nb_buckets * 2  # Capacity is hardcoded
    capacity = tf.minimum(length, capacity)
    tf.summary.scalar("dispatch_capacity", capacity, family="lsh")
    return expert_utils.TruncatingDispatcher(gates, capacity)

  def add_summary_capacity(x, prefix):
    # Monitor if capacity overflow
    x = x[0, ...]  # Take first batch/head
    x = tf.reduce_sum(x, axis=0)
    tf.summary.scalar(prefix + "_min", tf.reduce_min(x), family="lsh")
    tf.summary.scalar(prefix + "_max", tf.reduce_max(x), family="lsh")
    tf.summary.histogram(prefix + "capacity_distribution", x, family="lsh")
    for i in range(3):  # Show the first 3 buckets
      tf.summary.scalar("{}_{}".format(prefix, i), x[i], family="lsh")

  add_summary_capacity(gates_q, "q")
  add_summary_capacity(gates_k, "k")

  q_dispatcher = get_dispatcher(gates_q)
  k_dispatcher = get_dispatcher(gates_k)

  q = q_dispatcher.dispatch(q)
  k = k_dispatcher.dispatch(k)
  v = k_dispatcher.dispatch(v)

  # Bias of shape [batch*heads, nb_buckets, 1, capacity] broadcasted to every
  # queries
  bias = tf.expand_dims((k_dispatcher.nonpadding() - 1.0) * 1e9, 2)
  if mask_right:
    q_coordinate = to_float(
        tf.expand_dims(q_dispatcher.length_coordinate(), 3))
    k_coordinate = to_float(
        tf.expand_dims(k_dispatcher.length_coordinate(), 2))
    bias += to_float(tf.greater(k_coordinate, q_coordinate)) * -1e9
  # The sequence padding is not masked but is ignored on the next layers

  # q, k, v now have shape [batch*heads, nb_bucket, capacity, depth]
  # The buckets can be seen as different heads
  v_out = dot_product_attention(q, k, v, bias=bias)

  # Combine all buckets together to restore the original length
  return q_dispatcher.combine(v_out)


@expert_utils.add_name_scope()
def sparse_dot_product_attention_truncated(
    q,
    k,
    v,
    bi,  # Unused
    experts_params,
    use_map_fn=False,  # Unused
    mask_right=False,
):  # pylint: disable=unused-argument
  """Sparse multihead self attention.

  Perform an approximation of the full multihead attention by dispatching
  the tokens using their keys/values. Thus the attention matrix are only
  computed each times on a subset of the tokens.

  Notes:
   * The function don't perform scaling here (multihead_attention does
  the /sqrt(depth)).
   * The padding should have been removed (so batch size should be 1 but length
   contains the elements from all different batches)
   * Right now, only self attention is supported so length_q and length_kv
   should be identical and the function will add triangular mask.
   * If bi.order is not None, The bias is added inside this function to
   prevent attention to the future.

  Args:
    q (tf.Tensor): Queries of shape [batch, heads, length_q, depth_k]
    k (tf.Tensor): Keys of shape [batch, heads, length_q, depth_k]
    v (tf.Tensor): Values of shape [batch, heads, length_kv, depth_v]
    bi (BatchInfo): Contains the batch coordinates and sequence order
    experts_params (dict): Additional params for the local expert
    use_map_fn (bool): Use either tf.map_fn of python for loop to compute the
      heads separately
    mask_right (bool):
  Returns:
    tf.Tensor: Approximation of Softmax(Q.K) * V, of shape
      [batch, heads, length_q, depth_v]
  """
  # Currently depth is the same for for q and v
  batch_size, nb_heads, _, depth = common_layers.shape_list(q)

  total_loss = 0.0

  # Each head get its own dispatcher
  list_lsh = [LshGating(depth=depth, **experts_params) for _ in range(nb_heads)]

  @expert_utils.add_name_scope()
  def get_gates_head(x, add_first=False):
    """Return the gates for each heads of the current x.

    Args:
      x (tf.Tensor): of shape [batch, heads, length, depth]
      add_first (bool): if True, add the first element on each bucket

    Returns:
      tf.Tensor: gates of shape [batch, heads, length, num_buckets]
    """
    length = common_layers.shape_list(x)[2]

    # Invert heads/batch
    x = tf.transpose(x, perm=[1, 0, 2, 3])
    x = tf.reshape(x, [nb_heads, batch_size * length, depth])

    list_x = tf.unstack(x)  # list[tf.Tensor(shape=[batch * length, depth])]

    # Unstack heads
    list_gates = []
    # There might be a more optimized way to compute all heads at once
    for lsh, single_x in zip(list_lsh, list_x):
      # Each head get its own dispatcher
      gates = lsh.get_gates(single_x)
      nb_buckets = gates.get_shape().as_list()[-1]
      # Reshape to [batch, length, depth] but should consider sequence
      # padding in that case (also dispatch the padding)
      gates = tf.reshape(gates, [batch_size, length, nb_buckets])
      list_gates.append(gates)

    gates = tf.stack(list_gates)

    # Restore original shape
    gates = tf.reshape(gates, [nb_heads, batch_size, length, nb_buckets])
    gates = tf.transpose(gates, [1, 0, 2, 3])

    # Dispatch the first element to every gates to avoid empty buckets
    if add_first:
      gates = tf.maximum(gates,
                         tf.reshape(tf.one_hot([0], length), [1, 1, length, 1]))

    return gates

  gates_q = get_gates_head(q)
  gates_k = get_gates_head(k, add_first=True)

  # [batch, heads, length, depth] => [batch*heads, length, depth]
  q, k, v, gates_q, gates_k = [
      combine_first_two_dimensions(t) for t in (q, k, v, gates_q, gates_k)
  ]

  v_out = dot_product_batched_head(q, k, v, gates_q, gates_k, mask_right)

  # Restore original dimension
  v_out = tf.reshape(v_out, [batch_size, nb_heads, -1, depth])

  return v_out, total_loss / nb_heads


@expert_utils.add_var_scope()
def deconv_elems_1d(x, factor, out_depth=None):
  """Increase the length and change the dimensionality.

  Expand/project each positions of dim depth of the input into
  factor*tokens of dim out_depth

  Args:
    x (tf.Tensor): shape [batch_size, length, depth]
    factor (int): Multiplicative factor of each tokens.
    out_depth (int): Output depth (if None, keep depth constant)

  Returns:
    tf.Tensor: shape [batch_size, length*factor, out_depth]
  """
  out_depth = out_depth or x.get_shape().as_list()[-1]
  x = tf.expand_dims(x, 1)  # [batch_size, 1, length, depth]
  x = layers().Conv2DTranspose(
      filters=out_depth,
      kernel_size=(1, factor),
      strides=(1, factor),
      padding="valid",
      data_format="channels_last",
  )(x)  # [batch_size, 1, length*factor, out_depth]
  x = tf.squeeze(x, 1)  # [batch_size, length*factor, depth]
  return x


@expert_utils.add_var_scope()
def conv_elems_1d(x, factor, out_depth=None):
  """Decrease the length and change the dimensionality.

  Merge/restore/compress factors positions of dim depth of the input into
  a single position of dim out_depth.
  This is basically just a strided convolution without overlap
  between each strides. The original length has to be divided by factor.

  Args:
    x (tf.Tensor): shape [batch_size, length, depth]
    factor (int): Length compression factor.
    out_depth (int): Output depth

  Returns:
    tf.Tensor: shape [batch_size, length//factor, out_depth]
  """
  out_depth = out_depth or x.get_shape().as_list()[-1]
  # with tf.control_dependencies(  # Dynamic assertion
  #     [tf.assert_equal(tf.shape(x)[1] % factor, 0)]):
  x = tf.expand_dims(x, 1)  # [batch_size, 1, length, depth]
  x = layers().Conv2D(
      filters=out_depth,
      kernel_size=(1, factor),
      strides=(1, factor),
      padding="valid",
      data_format="channels_last",
  )(x)  # [batch_size, 1, length//factor, out_depth]
  x = tf.squeeze(x, 1)  # [batch_size, length//factor, depth]
  return x


@expert_utils.add_var_scope()
def local_reduction_attention(x, block_length, multihead_params):
  """Reduce the length dimension using self attention.

  Args:
    x (tf.Tensor): float32 of shape [batch, length, depth]
    block_length (int): Block length for local attention (Compression factor)
    multihead_params (dict): parameters for multihead attention

  Returns:
    tf.Tensor: Compressed tensor of shape [batch, length // factor, depth]
  """

  @expert_utils.add_name_scope()
  def dot_product_self_local_attention_flattened(q, k, v):
    """Strided block local self-attention.

    No overlap between the blocks.

    Args:
      q (tf.Tensor): shape [batch, heads, length, depth_k]
      k (tf.Tensor): shape [batch, heads, length, depth_k]
      v (tf.Tensor): shape [batch, heads, length, depth_v]

    Returns:
      tf.Tensor: shape [batch, heads, length, depth_v]
    """
    _, num_head, _, depth = q.get_shape().as_list()

    # Extract the blocks
    def pad_and_reshape(x):
      """Split the length dim into [num_block, block_length]."""
      length_x = common_layers.shape_list(x)[2]
      # Add some padding, but won't matter as the last block will never be
      # attended by the query (after compression)
      x = tf.pad(x, [[0, 0], [0, 0], [0, -length_x % block_length], [0, 0]])
      x = tf.reshape(
          x,
          [
              common_layers.shape_list(x)[0],  # Batch
              num_head,  # Head
              common_layers.shape_list(x)[2] // block_length,  # Num blocks
              block_length,  # Block length
              depth,  # Depth
          ])
      return x

    q, k, v = [pad_and_reshape(t) for t in (q, k, v)]

    # Perform attention on the flattened dot product
    logits = tf.matmul(q, k, transpose_b=True)
    logits = tf.reshape(
        logits,
        [
            common_layers.shape_list(logits)[0],  # Batch
            num_head,  # Head
            common_layers.shape_list(logits)[2],  # Num blocks
            block_length**2,  # Flatten last dimension
        ])
    weights = tf.nn.softmax(logits)
    weights = tf.reshape(
        weights,
        [
            common_layers.shape_list(weights)[0],  # Batch
            num_head,  # Head
            common_layers.shape_list(weights)[2],  # Num blocks
            block_length,
            block_length,  # Restore the block length dimension
        ])
    weights = tf.reduce_sum(weights, axis=3, keep_dims=True)  # Compress block
    v_out = tf.matmul(weights, v)  # [1, block_length] @ [block_length, depth]
    v_out = tf.squeeze(v_out, axis=3)
    return v_out

  return multihead_attention(
      x,
      None,
      bias=None,
      output_depth=x.get_shape().as_list()[-1],
      attention_type=dot_product_self_local_attention_flattened,
      **multihead_params)


@expert_utils.add_var_scope()
def multihead_self_attention_reduced(
    x,
    memory_antecedent=None,
    bias=None,
    factor=None,
    multihead_params=None,
    nonlinearity="none",
    reduction_type="conv",
    add_mask=True,
):
  """Reduce the length dimension by compressing with conv.

  Args:
    x (tf.Tensor): float32 of shape [batch, length, depth]
    memory_antecedent (tf.Tensor): Unsupported for now
    bias (tf.Tensor): Ignored
    factor (int): compression factor for the memory sequence
    multihead_params (dict): parameters for multihead attention
    nonlinearity (str): Add some non-linearity after the memory block
    reduction_type (str): type of compression
    add_mask (bool): If True, add the bias to prevent attention to the future

  Returns:
    (tf.Tensor): float32 of shape [batch, length, depth]

  Raises:
    ValueError: If reduction_type or nonlinearity is invalid
  """
  if not factor or not multihead_params:
    raise ValueError("factor and multihead_params should be set")
  if memory_antecedent is not None:
    raise NotImplementedError(
        "multihead_self_attention_reduced only works with self-attention")

  depth = x.get_shape().as_list()[-1]

  # Could try to have some overlap between the blocks but that would
  # create conv artifacts, would make it difficult to not attend to the future
  # within one group and the padding should be handled specially.

  # Reduce the memory dimension
  if reduction_type == "attention":
    memory_x = local_reduction_attention(x, factor, multihead_params)
  elif reduction_type == "conv":
    # With valid padding, the last block won't be computed (not attended anyway)
    memory_x = conv_elems_1d(x, factor)
  else:
    raise ValueError("Unknown reduction type {}".format(reduction_type))

  if nonlinearity == "silu":
    memory_x *= tf.nn.sigmoid(memory_x)
  elif nonlinearity != "none":
    raise ValueError("Unknown non linearity {}".format(nonlinearity))

  memory_x = tf.concat(
      # Add the first elem to make it attendable by everyone (otherwise the
      # first block cannot attend to anything)
      [x[:, :1, :], memory_x],
      axis=1,
  )

  # Construct the bias
  @expert_utils.add_name_scope()
  def construct_bias_vectors(t, axis):
    length = to_float(common_layers.shape_list(t)[1])
    length_coordinates = tf.range(length, dtype=tf.float32)
    length_coordinates = tf.expand_dims(length_coordinates, axis=axis)
    # [1, length_k] or [length_q, 1]
    return length_coordinates

  if add_mask:  # Create mask to prevent attention to the future
    bias = to_float(
        tf.greater(
            # Because we add the first elem to the memory block and it can be
            # attended by anyone,we don't need to add +1 anymore to prevent self
            # attention Use * factor to make sure the last tokens  of a block
            # cannot attend the block
            construct_bias_vectors(memory_x, 0) * factor,
            # +epsilon to avoid float equality
            construct_bias_vectors(x, 1) + 1e-3,
        )) * -1e9
    bias = tf.expand_dims(bias, axis=0)
    bias = tf.expand_dims(bias, axis=0)  # [1, 1, length_k, length_q]
  else:
    bias = None

  return multihead_attention(
      query_antecedent=x,
      memory_antecedent=memory_x,
      bias=bias,
      output_depth=depth,
      **multihead_params)


def scaled_dot_product_attention_simple(q, k, v, bias, name=None):
  """Scaled dot-product attention. One head. One spatial dimension.

  Args:
    q: a Tensor with shape [batch, length_q, depth_k]
    k: a Tensor with shape [batch, length_kv, depth_k]
    v: a Tensor with shape [batch, length_kv, depth_v]
    bias: optional Tensor broadcastable to [batch, length_q, length_kv]
    name: an optional string

  Returns:
    A Tensor.
  """
  with tf.variable_scope(
      name, default_name="scaled_dot_product_attention_simple"):
    scalar = tf.rsqrt(to_float(common_layers.shape_list(q)[2]))
    logits = tf.matmul(q * scalar, k, transpose_b=True)
    if bias is not None:
      logits += bias
    weights = tf.nn.softmax(logits, name="attention_weights")
    if common_layers.should_generate_summaries():
      tf.summary.image(
          "attention", tf.expand_dims(tf.pow(weights, 0.2), 3), max_outputs=1)
    return tf.matmul(weights, v)


_function_cache = {}


def multihead_self_attention_memory_efficient(x,
                                              bias,
                                              num_heads,
                                              head_size=None,
                                              epsilon=1e-6,
                                              forget=True,
                                              test_vars=None,
                                              name=None):
  """Multihead scaled-dot-product self-attention.

  Includes layer norm.

  Returns multihead-self-attention(layer_norm(x))

  Computes one attention head at a time to avoid exhausting memory.

  If forget=True, then forget all forwards activations and recompute on
  the backwards pass.

  Args:
    x: a Tensor with shape [batch, length, input_size]
    bias: an attention bias tensor broadcastable to [batch, 1, length, length]
    num_heads: an integer
    head_size: an optional integer - defaults to input_size/num_heads
    epsilon: a float, for layer norm
    forget: a boolean - forget forwards activations and recompute on backprop
    test_vars: optional tuple of variables for testing purposes
    name: an optional string

  Returns:
    A Tensor.
  """
  io_size = x.get_shape().as_list()[-1]
  if head_size is None:
    assert io_size % num_heads == 0
    head_size = io_size / num_heads

  def forward_internal(x, wqkv, wo, attention_bias, norm_scale, norm_bias):
    """Forward function."""
    n = common_layers.layer_norm_compute(x, epsilon, norm_scale, norm_bias)
    wqkv_split = tf.unstack(wqkv, num=num_heads)
    wo_split = tf.unstack(wo, num=num_heads)
    y = 0
    for h in range(num_heads):
      with tf.control_dependencies([y] if h > 0 else []):
        combined = tf.nn.conv1d(n, wqkv_split[h], 1, "SAME")
        q, k, v = tf.split(combined, 3, axis=2)
        o = scaled_dot_product_attention_simple(q, k, v, attention_bias)
        y += tf.nn.conv1d(o, wo_split[h], 1, "SAME")
    return y

  key = (
      "multihead_self_attention_memory_efficient %s %s" % (num_heads, epsilon))
  if not forget:
    forward_fn = forward_internal
  elif key in _function_cache:
    forward_fn = _function_cache[key]
  else:

    @function.Defun(compiled=True)
    def grad_fn(x, wqkv, wo, attention_bias, norm_scale, norm_bias, dy):
      """Custom gradient function."""
      with tf.control_dependencies([dy]):
        n = common_layers.layer_norm_compute(x, epsilon, norm_scale, norm_bias)
        wqkv_split = tf.unstack(wqkv, num=num_heads)
        wo_split = tf.unstack(wo, num=num_heads)
        deps = []
        dwqkvs = []
        dwos = []
        dn = 0
        for h in range(num_heads):
          with tf.control_dependencies(deps):
            combined = tf.nn.conv1d(n, wqkv_split[h], 1, "SAME")
            q, k, v = tf.split(combined, 3, axis=2)
            o = scaled_dot_product_attention_simple(q, k, v, attention_bias)
            partial_y = tf.nn.conv1d(o, wo_split[h], 1, "SAME")
            pdn, dwqkvh, dwoh = tf.gradients(
                ys=[partial_y],
                xs=[n, wqkv_split[h], wo_split[h]],
                grad_ys=[dy])
            dn += pdn
            dwqkvs.append(dwqkvh)
            dwos.append(dwoh)
            deps = [dn, dwqkvh, dwoh]
        dwqkv = tf.stack(dwqkvs)
        dwo = tf.stack(dwos)
        with tf.control_dependencies(deps):
          dx, dnorm_scale, dnorm_bias = tf.gradients(
              ys=[n], xs=[x, norm_scale, norm_bias], grad_ys=[dn])
        return (dx, dwqkv, dwo, tf.zeros_like(attention_bias), dnorm_scale,
                dnorm_bias)

    @function.Defun(
        grad_func=grad_fn, compiled=True, separate_compiled_gradients=True)
    def forward_fn(x, wqkv, wo, attention_bias, norm_scale, norm_bias):
      return forward_internal(x, wqkv, wo, attention_bias, norm_scale,
                              norm_bias)

    _function_cache[key] = forward_fn

  if bias is not None:
    bias = tf.squeeze(bias, 1)
  with tf.variable_scope(name, default_name="multihead_attention", values=[x]):
    # TODO(noam): it would be nice to save memory by casting x to float16
    # here, but this causes problems with the gradients.  Figure out if there
    # is a way to leave the gradients as float32.
    if test_vars is not None:
      wqkv, wo, norm_scale, norm_bias = list(test_vars)
    else:
      wqkv = tf.get_variable(
          "wqkv", [num_heads, 1, io_size, 3 * head_size],
          initializer=tf.random_normal_initializer(stddev=io_size**-0.5))
      wo = tf.get_variable(
          "wo", [num_heads, 1, head_size, io_size],
          initializer=tf.random_normal_initializer(
              stddev=(head_size * num_heads)**-0.5))
      norm_scale, norm_bias = common_layers.layer_norm_vars(io_size)
    y = forward_fn(x, wqkv, wo, bias, norm_scale, norm_bias)
    y.set_shape(x.get_shape())
    return y


multihead_attention_sparse_dot_prod = functools.partial(
    multihead_attention, attention_type=sparse_dot_product_attention)

multihead_attention_sparse_truncated = functools.partial(
    multihead_attention, attention_type=sparse_dot_product_attention_truncated)
