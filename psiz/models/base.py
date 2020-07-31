# -*- coding: utf-8 -*-
# Copyright 2020 The PsiZ Authors. All Rights Reserved.
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
# ============================================================================
"""Module of psychological embedding models.

Classes:
    Proxy: Proxy class for embedding model.
    PsychologicalEmbedding: Abstract base class for a psychological
        embedding model.

Functions:
    load_model: Load a hdf5 file, that was saved with the `save`
        class method, as a PsychologicalEmbedding object.

"""

import copy
import json
import os
from pathlib import Path
import warnings

import h5py
import numpy as np
import tensorflow as tf
from tensorflow.keras import backend as K
from tensorflow.python.keras.engine import data_adapter
from tensorflow.python.eager import backprop
import tensorflow_probability as tfp

import psiz.keras.layers
import psiz.trials


class Proxy(object):
    """Convenient proxy class for a psychological embedding model.

    The embedding procedure jointly infers three components. First, the
    embedding algorithm infers a stimulus representation denoted by the
    variable z. Second, the embedding algorithm infers the variables
    governing the similarity kernel, denoted theta. Third, the
    embedding algorithm infers a set of attention weights if there is
    more than one group.

    Methods: TODO
        compile: Assign a optimizer, loss and regularization function
            for the optimization procedure.
        fit: Fit the embedding model using the provided observations.
        evaluate: Evaluate the embedding model using the provided
            observations.
        similarity: Return the similarity between provided points.
        distance: Return the (weighted) minkowski distance between
            provided points.
        set_log: Adjust the TensorBoard logging behavior.
        save: Save the embedding model as an hdf5 file.

    Attributes: TODO
        n_stimuli: The number of unique stimuli in the embedding.
        n_dim: The dimensionality of the embedding.
        n_group: The number of distinct groups in the embedding.
        z: A dictionary with the keys 'value', 'trainable'. The key
            'value' contains the actual embedding points. The key
            'trainable' is a boolean flag that determines whether
            the embedding points are inferred during inference.
        theta: Dictionary containing data about the parameter values
            governing the similarity kernel. The dictionary contains
            the variable names as keys at the first level. For each
            variable, there is an additional dictionary containing
            the keys 'value', 'trainable', and 'bounds'. The key
            'value' indicates the actual value of the parameter. The
            key 'trainable' is a boolean flag indicating whether the
            variable is trainable during inferene. The key 'bounds'
            indicates the bounds of the parameter during inference. The
            bounds are specified using a list of two items where the
            first item indicates the lower bound and the second item
            indicates the upper bound. Use None to indicate no bound.
        phi: Dictionary containing data about the group-specific
            parameter values. These parameters are only trainable if
            there is more than one group. The dictionary contains the
            parameter names as keys at the first level. For each
            parameter name, there is an additional dictionary
            containing the keys 'value' and 'trainable'. The key
            'value' indicates the actual value of the parameter. The
            key 'trainable' is a boolean flag indicating whether the
            variable is trainable during inference. The free parameter
            `w` governs dimension-wide weights.
        log_freq: The number of epochs to wait between log entries.

    """

    def __init__(self, model):
        """Initialize.

        Arguments:
            model: A TensorFlow model.

        """
        super().__init__()
        self.model = model

        # Unsaved attributes.
        self.log_freq = 10

    @property
    def n_stimuli(self):
        """Getter method for n_stimuli."""
        return self.model.n_stimuli

    @property
    def n_dim(self):
        """Getter method for n_dim."""
        return self.model.n_dim

    @property
    def n_group(self):
        """Getter method for n_group."""
        return self.model.n_group

    @property
    def z(self):
        """Getter method for `z`."""
        z = self.model.stimuli.embeddings
        if isinstance(z, tfp.distributions.Distribution):
            z = z.mode()  # NOTE: This will not work for all distributions.
        z = z.numpy()
        if self.model.stimuli.mask_zero:
            if len(z.shape) == 2:
                z = z[1:]
            else:
                z = z[:, 1:]
        return z

    @property
    def w(self):
        """Getter method for `w`."""
        if hasattr(self.model.kernel, 'attention'):
            w = self.model.kernel.attention.embeddings
            if isinstance(w, tfp.distributions.Distribution):
                if isinstance(w.distribution, tfp.distributions.LogitNormal):
                    # For logit-normal distribution, use median instead of
                    # mode. 
                    # `median = logistic(loc)`.
                    w = tf.math.sigmoid(w.distribution.loc)
                else:
                    w = w.mode()  # NOTE: The mode may be undefined.
            w = w.numpy()
            if self.model.kernel.attention.mask_zero:
                    w = w[1:]
        else:
            w = np.ones([1, self.n_dim])
        return w

    @property
    def phi(self):
        """Getter method for `phi`."""
        d = {
            'w': self.w
        }
        return d

    @property
    def theta(self):
        """Getter method for `theta`."""
        d = {}
        for k, v in self.model.theta.items():
            d[k] = v.numpy()
        return d

    def _broadcast_ready(self, input, rank):
        """Create necessary singleton dimensions for `input`."""
        n_increase = rank - len(input.shape)
        for i_rank in range(n_increase):
            input.expand_dims(input, axis=1)
        return input

    def similarity(self, z_q, z_r, group_id=None):
        """Return similarity between two lists of points.

        Similarity is determined using the similarity kernel and the
        current similarity parameters. This method implements the
        logic for handling arguments of different shapes.

        The arguments `z_q` and `z_r` must be at least rank 2, and have
        the same rank.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, [m, n, ...] n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, [m, n, ...] n_dim)
            group_id (optional): The group ID for each sample. Can be a
                scalar or an array of shape = (n_trial,).

        Returns:
            The corresponding similarity between rows of embedding
                points.

        """
        # Prepare inputs to exploit broadcasting.
        n_trial = z_q.shape[0]
        if group_id is None:
            group_id = np.zeros((n_trial), dtype=np.int32)
        else:
            if np.isscalar(group_id):
                group_id = group_id * np.ones((n_trial), dtype=np.int32)
            else:
                group_id = group_id.astype(dtype=np.int32)
        group_id = np.expand_dims(group_id, axis=1)

        # Pass through kernel function.
        sim_qr = self.model.kernel([
            tf.constant(z_q, dtype=K.floatx()),
            tf.constant(z_r, dtype=K.floatx()),
            tf.constant(group_id, dtype=tf.int32)
        ]).numpy()
        return sim_qr

    def distance(self, z_q, z_r, group_id=None):
        """Return distance between two lists of points.

        Distance is determined using the weighted Minkowski metric.
        This method implements the logic for handling arguments of
        different shapes.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim, [1, n_sample])
            z_r: A set of embedding points.
                shape = (n_trial, n_dim, [n_reference, n_sample])
            group_id (optional): The group ID for each sample. Can be a
                scalar or an array of shape = (n_trial,).

        Returns:
            The corresponding similarity between rows of embedding
                points.

        """
        n_trial = z_q.shape[0]
        # Prepare inputs to exploit broadcasting.
        if group_id is None:
            group_id = np.zeros((n_trial), dtype=np.int32)
        else:
            if np.isscalar(group_id):
                group_id = group_id * np.ones((n_trial), dtype=np.int32)
            else:
                group_id = group_id.astype(dtype=np.int32)
        attention = self.w[group_id, :]  # TODO brittle assumption
        attention = self._broadcast_ready(attention, rank=len(z_q.shape))  # TODO verify

        # TODO brittle assumption
        d_qr = self.model.kernel.distance([
            tf.constant(z_q, dtype=K.floatx()),
            tf.constant(z_r, dtype=K.floatx()),
            tf.constant(attention, dtype=K.floatx())
        ]).numpy()
        return d_qr

    # def _check_obs(self, obs):
    #     """Check observerations.

    #     Arguments:
    #         obs: A psiz.trials.RankObservations object.

    #     Raises:
    #         ValueError

    #     """
    #     n_group_obs = np.max(obs.group_id) + 1
    #     if n_group_obs > self.n_group:
    #         raise ValueError(
    #             "The provided observations contain data from at least {0}"
    #             " groups. The present model only supports {1}"
    #             " group(s).".format(
    #                 n_group_obs, self.n_group
    #             )
    #         )

    def compile(self, **kwargs):
        """Configure the model for training.

        Compile defines the loss function, the optimizer and the
        metrics.

        Arguments:
            **kwargs: Key-word arguments passed to the TensorFlow
            model's `compile` method.

        """
        self.model.compile(**kwargs)

    def fit(
            self, obs_train, batch_size=None, validation_data=None,
            n_restart=3, n_record=1, do_init=False, monitor='loss',
            compile_kwargs={}, **kwargs):
        """Fit the free parameters of the embedding model.

        This convenience function formats the observations as
        appropriate Dataset(s) and sets up a Restarter object to track
        restarts.

        Arguments:
            obs_train: An RankObservations object representing the
                observed data used to train the model.
            batch_size (optional): The batch size to use for the
                training step.
            validation_data (optional): An RankObservations object
                representing the observed data used to validate the
                model.
            n_restart (optional): The number of independent restarts to
                perform.
            n_record (optional): The number of top-performing restarts
                to record.
            monitor (optional): The value to monitor and select
                restarts.
            kwargs (optional): Additional key-word arguments to be
                passed to the model's `fit` method.

        Returns:
            restart_record: A psiz.restart.FitTracker object.

        """
        # Determine batch size.
        n_obs_train = obs_train.n_trial
        if batch_size is None:
            batch_size_train = n_obs_train
        else:
            batch_size_train = np.minimum(batch_size, n_obs_train)

        # Create TensorFlow training Dataset.
        # self._check_obs(obs_train)
        # Format as TensorFlow dataset.
        ds_obs_train = obs_train.as_dataset()
        ds_obs_train = ds_obs_train.shuffle(
            buffer_size=n_obs_train, reshuffle_each_iteration=True
        )
        ds_obs_train = ds_obs_train.batch(
            batch_size_train, drop_remainder=False
        )

        # Create TensorFlow validation Dataset (if necessary).
        if validation_data is not None:
            # self._check_obs(validation_data)
            ds_obs_val = validation_data.as_dataset()
            n_obs_val = validation_data.n_trial
            # Format as TensorFlow dataset.
            ds_obs_val = ds_obs_val.batch(
                n_obs_val, drop_remainder=False
            )
        else:
            ds_obs_val = None

        # Handle restarts.
        restarter = psiz.restart.Restarter(
            self.model, compile_kwargs=compile_kwargs, monitor=monitor,
            n_restart=n_restart, n_record=n_record, do_init=do_init
        )
        restart_record = restarter.fit(
            x=ds_obs_train, validation_data=ds_obs_val, **kwargs
        )
        self.model = restarter.model

        return restart_record

    def evaluate(self, obs, batch_size=None, **kwargs):
        """Evaluate observations using the current state of the model.

        This convenience function formats the observations as
        an appropriate Dataset. Observations are evaluated in "test"
        mode. This means that regularization terms are not included in
        the loss.

        Arguments:
            obs: A RankObservations object representing the observed
                data.
            batch_size (optional): Integer indicating the batch size.
            kwargs (optional): Additional key-word arguments for
                evaluate.

        Returns:
            loss: The average loss per observation. Loss is defined as
                the negative loglikelihood.

        """
        # self._check_obs(obs)
        ds_obs = obs.as_dataset()

        if batch_size is None:
            batch_size = obs.n_trial

        ds_obs = ds_obs.batch(batch_size, drop_remainder=False)
        metrics = self.model.evaluate(x=ds_obs, **kwargs)
        # TODO First call to evaluate isn't correct. Work-around is to
        # call it twice.
        metrics_2 = self.model.evaluate(x=ds_obs, **kwargs)
        return metrics_2

    # def predict(self, docket, batch_size=None, **kwargs):
    #     """Predict outcomes given current state of model.

    #     Arguments:
    #         docket: A docket of trials.
    #         batch_size (optional): Integer indicating batch size.

    #     Returns:
    #         Numpy array(s) of predictions.

    #     """
    #     ds_docket = docket.as_dataset()

    #     if batch_size is None:
    #         batch_size = docket.n_trial

    #     ds_docket = ds_docket.batch(batch_size, drop_remainder=False)
    #     predictions = self.model.predict(x=ds_docket, **kwargs)
    #     return predictions.numpy()

    def save(self, filepath, overwrite=False):
        """Save the model."""
        self.model.save(filepath, overwrite=overwrite)

    # def subset(self, idx):  TODO DELETE
    #     """Return subset of embedding."""
    #     emb = self.clone()
    #     raise(NotImplementedError)
    #     # TODO CRITICAL, must handle changes to embedding layer
    #     emb.z = emb.z[idx, :] # must use assign
    #     emb.n_stimuli = emb.z.shape[0]
    #     return emb

    def clone(self, custom_objects={}):
        """Clone model."""
        # TODO Test
        # Create topology.
        with tf.keras.utils.custom_object_scope(custom_objects):
            new_model = self.model.from_config(self.model.get_config())

        # Save weights.
        fp_weights = '/tmp/psiz/clone'
        self.model.save_weights(fp_weights, overwrite=True)

        # Set weights of new model.
        new_model.load_weights(fp_weights)

        # Wrap in proxy Class.
        proxy_model = Proxy(model=new_model)

        # Compile to model. TODO is this too brittle?
        # if self.model.loss is not None:
        #     proxy_model.compile(
        #         loss=self.model.loss, optimizer=self.model.optimizer
        #     )

        # Other attributes.
        proxy_model.log_freq = self.log_freq

        return proxy_model


class PsychologicalEmbedding(tf.keras.Model):
    """A pscyhological embedding model.

    This model can be subclassed to infer a psychological embedding
    from different types similarity judgment data.

    Attributes:
        embedding: An embedding layer.
        kernel: A kernel layer.
        behavior: A behavior layer.
        n_stimuli: The number of stimuli.
        n_dim: The dimensionality of the embedding.
        n_group: The number of groups.
        n_sample_test: The number of samples to use during test. This
            attribute is only relevant if using probabilistic layers.
            Otherwise it should be kept at the default value of 1.

    """

    def __init__(
            self, stimuli=None, kernel=None, behavior=None,
            n_sample_test=1, **kwargs):
        """Initialize.

        Arguments:
            stimuli: An embedding layer representing the stimuli. Must
                agree with n_stimuli, n_dim, n_group.
            attention (optional): An attention layer. Must agree with
                n_stimuli, n_dim, n_group.
            distance (optional): A distance kernel function layer.
            similarity (optional): A similarity function layer.
            n_sample_test (optional): The number of samples from
                posterior to use during evaluation.
            kwargs:  Additional key-word arguments.

        Raises:
            ValueError: If arguments are invalid.

        """
        super().__init__(**kwargs)

        # Assign layers.
        self.stimuli = stimuli
        self.kernel = kernel
        self.behavior = behavior

        self.n_sample_test = n_sample_test

        if type(self.stimuli) is psiz.keras.layers.EmbeddingGroup:
            self.group_embedding = True
        else:
            self.group_embedding = False

        # Create convenience pointer to kernel parameters.
        self.theta = self.kernel.theta

    @property
    def n_stimuli(self):
        """Getter method for `n_stimuli`."""
        n_stimuli = self.stimuli.input_dim
        if self.stimuli.mask_zero:
            n_stimuli = n_stimuli - 1
        return n_stimuli

    @property
    def n_dim(self):
        """Getter method for `n_dim`."""
        return self.stimuli.output_dim

    @property
    def n_group(self):
        """Getter method for `n_group`."""
        stimuli_n_group = 1
        if hasattr(self.stimuli, 'n_group'):
            stimuli_n_group = self.stimuli.n_group
        behavior_n_group = 1  # TODO add behavior.n_group to list
        return np.max([
            stimuli_n_group, self.kernel.n_group, behavior_n_group
        ])

    def train_step(self, data):
        """Logic for one training step.

        Arguments:
            data: A nested structure of `Tensor`s.

        Returns:
            A `dict` containing values that will be passed to
            `tf.keras.callbacks.CallbackList.on_train_batch_end`. Typically,
            the values of the `Model`'s metrics are returned. Example:
            `{'loss': 0.2, 'accuracy': 0.7}`.

        """
        data = data_adapter.expand_1d(data)
        x, y, sample_weight = data_adapter.unpack_x_y_sample_weight(data)

        # NOTE: During computation of gradients, IndexedSlices are
        # created which generates a TensorFlow warning. I cannot
        # find an implementation that avoids IndexedSlices. The
        # following catch environment silences the offending
        # warning.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                'ignore', category=UserWarning, module=r'.*indexed_slices'
            )
            with backprop.GradientTape() as tape:
                y_pred = self(x, training=True)
                loss = self.compiled_loss(
                    y, y_pred, sample_weight, regularization_losses=self.losses
                )
            # Custom training steps:
            trainable_variables = self.trainable_variables
            gradients = tape.gradient(loss, trainable_variables)
            # NOTE: There is an open issue for using constraints with
            # embedding-like layers (e.g., tf.keras.layers.Embedding,
            # psiz.keras.layers.GroupAttention), see
            # https://github.com/tensorflow/tensorflow/issues/33755.
            # There are also issues when using Eager Execution. A
            # work-around is to convert the problematic gradients, which
            # are returned as tf.IndexedSlices, into dense tensors.
            for idx, grad in enumerate(gradients):
                if gradients[idx].__class__.__name__ == 'IndexedSlices':
                    gradients[idx] = tf.convert_to_tensor(
                        gradients[idx]
                    )

        self.optimizer.apply_gradients(zip(
            gradients, trainable_variables)
        )

        self.compiled_metrics.update_state(y, y_pred, sample_weight)
        return {m.name: m.result() for m in self.metrics}

    def test_step(self, data):
        """The logic for one evaluation step.

        Arguments:
            data: A nested structure of `Tensor`s.

        Returns:
            A `dict` containing values that will be passed to
            `tf.keras.callbacks.CallbackList.on_train_batch_end`.
            Typically, the values of the `Model`'s metrics are
            returned.

        """
        data = data_adapter.expand_1d(data)
        x, y, sample_weight = data_adapter.unpack_x_y_sample_weight(data)

        # NOTE: The standard prediction is performmed with one sample. To
        # accommodate variational inference, the log prob of the data is
        # computed by averaging samples from the model:
        # p(heldout | train) = int_model p(heldout|model) p(model|train)
        #                   ~= 1/n * sum_{i=1}^n p(heldout | model_i)
        # where model_i is a draw from the posterior p(model|train).
        y_pred = tf.stack([
            self(x, training=False)
            for _ in range(self.n_sample_test)
        ], axis=0)
        y_pred = tf.reduce_mean(y_pred, axis=0)

        # Updates stateful loss metrics.
        regularization_losses = (
            tf.reduce_sum(self.losses) / self.n_sample_test
        )
        self.compiled_loss(
            y, y_pred, sample_weight,
            regularization_losses=[regularization_losses]
        )

        self.compiled_metrics.update_state(y, y_pred, sample_weight)
        return {m.name: m.result() for m in self.metrics}

    def get_config(self):
        """Return model configuration."""
        layer_configs = {
            'stimuli': tf.keras.utils.serialize_keras_object(
                self.stimuli
            ),
            'kernel': tf.keras.utils.serialize_keras_object(
                self.kernel
            ),
            'behavior': tf.keras.utils.serialize_keras_object(self.behavior)
        }

        config = {
            'name': self.name,
            'class_name': self.__class__.__name__,
            'n_sample_test': self.n_sample_test,
            'layers': copy.deepcopy(layer_configs)
        }
        return config

    @classmethod
    def from_config(cls, config):
        """Create model from configuration.

        Arguments:
            config: A hierarchical configuration dictionary.

        Returns:
            An instantiated and configured TensorFlow model.

        """
        model_config = copy.deepcopy(config)
        model_config.pop('class_name', None)

        # Deserialize layers.
        layer_configs = model_config.pop('layers', None)
        built_layers = {}
        for layer_name, layer_config in layer_configs.items():
            layer = tf.keras.layers.deserialize(layer_config)
            # Convert old saved models.
            if layer_name == 'embedding':
                layer_name = 'stimuli'
            built_layers[layer_name] = layer

        model_config.update(built_layers)
        return cls(**model_config)

    def save(self, filepath, overwrite=False):
        """Save the PsychologialEmbedding model.

        Ideally we would use TensorFlow model's defualt save
        method. Unfortunately, this does not always work for the
        sub-classed model appraoch. A custom implementation is used
        here.

        Arguments:
            filepath: String specifying the path to save the model.
            overwrite (optional): Whether to overwrite exisitng files.

        """
        # Make directory.
        Path(filepath).mkdir(parents=True, exist_ok=overwrite)

        fp_config = os.path.join(filepath, 'config.h5')
        fp_weights = os.path.join(filepath, 'weights')

        # TODO Guarantee types during get_config() method call, making
        # this recursive check unnecessary.
        def _convert_to_64(d):
            for k, v in d.items():
                if isinstance(v, np.float32):
                    if 'name' in d:
                        component_name = d['name'] + ':' + k
                    else:
                        component_name = k
                    print(
                        'WARNING: Model component `{0}` had type float32. Please'
                        ' check the corresponding get_config method for'
                        ' appropriate float casting.'.format(component_name)
                    )
                    d[k] = float(v)
                if isinstance(v, np.int64):
                    if 'name' in d:
                        component_name = d['name'] + ':' + k
                    else:
                        component_name = k
                    print(
                        'WARNING: Model component `{0}` had type int64. Please'
                        ' check the corresponding get_config method for'
                        ' appropriate int casting.'.format(component_name)
                    )
                    d[k] = int(v)
                elif isinstance(v, dict):
                    d[k] = _convert_to_64(v)
            return d

        # Save configuration.
        model_config = self.get_config()
        model_config = _convert_to_64(model_config)
        json_model_config = json.dumps(model_config)
        json_loss_config = json.dumps(
            tf.keras.losses.serialize(self.loss)
        )
        optimizer_config = tf.keras.optimizers.serialize(self.optimizer)
        # HACK: numpy.float32 is not serializable
        for k, v in optimizer_config['config'].items():
            if isinstance(v, np.float32):
                optimizer_config['config'][k] = float(v)
        json_optimizer_config = json.dumps(optimizer_config)
        f = h5py.File(fp_config, "w")
        f.create_dataset('model_type', data='psiz')
        f.create_dataset('psiz_version', data='0.4.0')
        f.create_dataset('config', data=json_model_config)
        f.create_dataset('loss', data=json_loss_config)
        f.create_dataset('optimizer', data=json_optimizer_config)
        f.close()

        # Save weights.
        self.save_weights(
            fp_weights, overwrite=overwrite, save_format='tf'
        )


def load_model(filepath, custom_objects={}, compile=False):
    """Load embedding model saved via the save method.

    Arguments:
        filepath: The location of the hdf5 file to load.
        custom_objects (optional): A dictionary mapping the string
            class name to the Python class
        compile: Boolean indicating if model should be compiled.

    Returns:
        A TensorFlow Keras model.

    Raises:
        ValueError

    """
    # Check if directory.
    if os.path.isdir(filepath):
        # NOTE: Ideally we could call TensorFlow's `load_model` method as used
        # in the snippet below, but our sub-classed model does not play
        # nice.
        # with tf.keras.utils.custom_object_scope(custom_objects):
        #     model = tf.keras.models.load_model(filepath, compile=compile)
        # return Proxy(model=model)
        # Storage format for psiz_version >= 0.4.0
        fp_config = os.path.join(filepath, 'config.h5')
        fp_weights = os.path.join(filepath, 'weights')

        # Load configuration.
        f = h5py.File(fp_config, 'r')
        psiz_version = f['psiz_version'][()]
        config = json.loads(f['config'][()])
        loss_config = json.loads(f['loss'][()])
        optimizer_config = json.loads(f['optimizer'][()])

        model_class_name = config.get('class_name')
        # Load model.
        if model_class_name in custom_objects:
            model_class = custom_objects[model_class_name]
        else:
            model_class = getattr(psiz.models, model_class_name)
        with tf.keras.utils.custom_object_scope(custom_objects):
            model = model_class.from_config(config)
            loss = tf.keras.losses.deserialize(loss_config)
            optimizer = tf.keras.optimizers.deserialize(optimizer_config)

        if compile:
            model.compile(loss=loss, optimizer=optimizer)
        # Build lazy layers in order to set weights.
        model.stimuli.build(input_shape=[None, None, None])

        # Load weights.
        model.load_weights(fp_weights).expect_partial()

        # emb = Proxy(model=model)
        emb = model
        
    else:
        raise ValueError(
            'The argument `filepath` must be a directory. The provided'
            ' {0} is not a directory.'.format(filepath)
        )

    return emb