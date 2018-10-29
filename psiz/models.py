# -*- coding: utf-8 -*-
# Copyright 2018 The PsiZ Authors. All Rights Reserved.
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
# ==============================================================================

"""Module of psychological embedding models.

Classes:
    PsychologicalEmbedding: Abstract base class for embedding model.
    Exponential: Embedding model using an exponential family similarity
        kernel.
    HeavyTailed: Embedding model using a heavy-tailed similarity
        kernel.
    StudentsT: Embedding model using a Student's t similarity kernel.

Functions:
    load_embedding: Load a hdf5 file, that was saved with the `save`
        class method, as a PsychologicalEmbedding object.

Todo:
    - method 'convert' takes z: shape=(n_stimuli, n_dim, n_sample) and
        group_id scalar, a returns z after applying the group specific
        transformations to z so that further calls to similarity or
        distance would use basic settings (e.g., uniform attention
        weights).
    - implement warm restarts (primarily embedding aspect)
    - document how to do warm restarts (warm restarts are sequential
      and can be created by the user using a for loop and fit with
      init_mode='warm')
    - document broadcasting in similarity function
    - document meaning of cold, warm, and exact initialization
    - document default tensorboard log location
        i.e., /tmp/psiz/tensorboard_logs/
    - expose optimizer
    - MAYBE ParameterSet, Theta and Phi object class
    - MAYBE parallelization during fitting


"""

import sys
from abc import ABCMeta, abstractmethod
import warnings
import copy
from random import randint

import h5py
import numpy as np
import numpy.ma as ma
import pandas as pd
from scipy.spatial.distance import pdist, squareform
from sklearn.model_selection import StratifiedKFold
from sklearn import mixture
import tensorflow as tf
from keras.initializers import Initializer
from tensorflow import random_uniform, random_normal
from tensorflow.contrib.distributions import Dirichlet

from psiz.utils import elliptical_slice

FLOAT_X = tf.float32  # TODO


class PsychologicalEmbedding(object):
    """Abstract base class for psychological embedding algorithm.

    The embedding procedure jointly infers three components. First, the
    embedding algorithm infers a stimulus representation denoted z.
    Second, the embedding algorithm infers the similarity kernel
    parameters of the concrete class. The set of similarity kernel
    parameters is denoted theta. Third, the embedding algorithm infers
    a set of attention weights if there is more than one group.

    Methods:
        fit: Fit the embedding model using the provided observations.
        evaluate: Evaluate the embedding model using the provided
            observations.
        similarity: Return the similarity between provided points.
        similarity_matrix: Return the similarity matrix characterizing
            the embedding.
        freeze: Freeze the free parameters of an embedding model.
        thaw: Make free parameters trainable.
        probability: Return the probability of the possible outcomes
            for each trial.
        log_likelihood: Return the log-likelihood of a set of
            observations.
        posterior_samples: Sample from the posterior distribution.
        set_log: Adjust the TensorBoard logging behavior.
        save: Save the embedding model as an hdf5 file.

    Attributes:
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
            indicates the bounds of the paramter during inference. The
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
            phi_1 governs dimension-wide weights.

    Notes:
        The methods fit, freeze, thaw, and set_log modify the state of
            the PsychologicalEmbedding object.
        The abstract methods _init_theta,
            _get_similarity_parameters_cold,
            _get_similarity_parameters_warm, and _tf_similarity must be
            implemented by each concrete class.

    """

    __metaclass__ = ABCMeta

    def __init__(self, n_stimuli, n_dim=2, n_group=1):
        """Initialize.

        Arguments:
            n_stimuli: An integer indicating the total number of unique
                stimuli that will be embedded. This must be equal to or
                greater than three.
            n_dim (optional): An integer indicating the dimensionalty
                of the embedding. Must be equal to or greater than one.
            n_group (optional): An integer indicating the number of
                different population groups in the embedding. A
                separate set of attention weights will be inferred for
                each group. Must be equal to or greater than one.

        Raises:
            ValueError

        """
        if (n_stimuli < 3):
            raise ValueError("There must be at least three stimuli.")
        self.n_stimuli = n_stimuli
        if (n_dim < 1):
            warnings.warn("The provided dimensionality must be an integer \
                greater than 0. Assuming user requested 2 dimensions.")
            n_dim = 2
        self.n_dim = n_dim
        if (n_group < 1):
            warnings.warn("The provided n_group must be an integer greater \
                than 0. Assuming user requested 1 group.")
            n_group = 1
        self.n_group = n_group

        # Initialize model components.
        self.z = self._init_z()
        self.theta = self._init_theta()
        self.phi = self._init_phi()

        # Default inference settings.
        # self.batch_size = 2048
        self.lr = 0.001
        self.max_n_epoch = 5000
        self.patience_stop = 10
        self.patience_reduce = 2

        # Default TensorBoard log attributes.
        self.do_log = False
        self.log_dir = '/tmp/psiz/tensorboard_logs/'

        super().__init__()

    def _init_z(self):
        """Return initialized embedding points.

        Initialize random embedding points using a multivariate
            Gaussian.
        """
        mean = np.ones((self.n_dim))
        cov = .03 * np.identity(self.n_dim)
        z = {}
        z['value'] = np.random.multivariate_normal(
            mean, cov, (self.n_stimuli)
        )
        z['trainable'] = True
        return z

    @abstractmethod
    def _init_theta(self):
        """Return dictionary of default theta parameters.

        Returns:
            Dictionary of theta parameters.

        """
        pass

    def _init_phi(self):
        """Return initialized phi.

        Initialize group-specific free parameters.
        """
        phi_1 = np.ones((self.n_group, self.n_dim), dtype=np.float32)
        if self.n_group is 1:
            is_trainable = False
        else:
            is_trainable = True
        phi = dict(
            phi_1=dict(value=phi_1, trainable=is_trainable)
        )
        return phi

    def _check_z(self, z):
        """Check argument `z`.

        Raises:
            ValueError

        """
        if z.shape[0] != self.n_stimuli:
            raise ValueError(
                "Input 'z' does not have the appropriate shape (number of \
                stimuli).")
        if z.shape[1] != self.n_dim:
            raise ValueError(
                "Input 'z' does not have the appropriate shape \
                (dimensionality).")

    def _check_phi_1(self, attention):
        """Check argument `phi_1`.

        Raises:
            ValueError

        """
        if attention.shape[0] != self.n_group:
            raise ValueError(
                "Input 'attention' does not have the appropriate shape \
                (number of groups).")
        if attention.shape[1] != self.n_dim:
            raise ValueError(
                "Input 'attention' does not have the appropriate shape \
                (dimensionality).")

    def _set_theta(self, theta):
        """State changing method sets algorithm-specific parameters.

        This method encapsulates the setting of algorithm-specific free
        parameters governing the similarity kernel.

        Arguments:
            theta: A dictionary of algorithm-specific parameter names
                and corresponding values.
        """
        for param_name in theta:
            self.theta[param_name]['value'] = theta[param_name]['value']

    def _get_similarity_parameters(self, init_mode):
        """Return a dictionary of TensorFlow variables.

        This method encapsulates the creation of algorithm-specific
        free parameters governing the similarity kernel.

        Arguments:
            init_mode: A string indicating the initialization mode.
                Valid options are 'cold', 'warm', and 'exact'.

        Returns:
            tf_theta: A dictionary of algorithm-specific TensorFlow
                variables.

        """
        with tf.variable_scope("similarity_params"):
            tf_theta = {}
            if init_mode is 'exact':
                tf_theta = self._get_similarity_parameters_exact()
            elif init_mode is 'warm':
                # tf_theta = self._get_similarity_parameters_exact()  # TODO
                tf_theta = self._get_similarity_parameters_cold()
            else:
                tf_theta = self._get_similarity_parameters_cold()

            # If a parameter is untrainable, set the parameter value to the
            # value in the class attribute theta.
            for param_name in self.theta:
                if not self.theta[param_name]['trainable']:
                    tf_theta[param_name] = tf.get_variable(
                        param_name, [1], dtype=FLOAT_X,
                        initializer=tf.constant_initializer(
                            self.theta[param_name]['value'], dtype=FLOAT_X
                        ),
                        trainable=False
                    )
        return tf_theta

    def _get_similarity_parameters_exact(self):
        """Return a dictionary.

        Returns:
            tf_theta: A dictionary of algorithm-specific TensorFlow
                variables.

        """
        tf_theta = {}
        for param_name in self.theta:
            if self.theta[param_name]['trainable']:
                tf_theta[param_name] = tf.get_variable(
                    param_name, [1], dtype=FLOAT_X,
                    initializer=tf.constant_initializer(
                        self.theta[param_name]['value'], dtype=FLOAT_X
                    ),
                    trainable=True
                )
        return tf_theta

    @abstractmethod
    def _get_similarity_parameters_cold(self):
        """Return a dictionary of TensorFlow parameters.

        Parameters are initialized by sampling from a relatively large
        set.

        Returns:
            tf_theta: A dictionary of algorithm-specific TensorFlow
                variables.

        """
        pass

    def _get_similarity_constraints(self, tf_theta):
        """Return a TensorFlow group of parameter constraints.

        Returns:
            tf_theta_bounds: A TensorFlow operation that imposes
                boundary constraints on the algorithm-specific free
                parameters during inference.

        """
        constraint_list = []
        for param_name in self.theta:
            bounds = self.theta[param_name]['bounds']
            if bounds[0] is not None:
                # Add lower bound.
                constraint_list.append(
                    tf_theta[param_name].assign(tf.maximum(
                        tf.constant(bounds[0], dtype=FLOAT_X),
                        tf_theta[param_name])
                    )
                )
            if bounds[1] is not None:
                # Add upper bound.
                constraint_list.append(
                    tf_theta[param_name].assign(tf.minimum(
                        tf.constant(bounds[1], dtype=FLOAT_X),
                        tf_theta[param_name])
                    )
                )
        tf_theta_bounds = tf.group(*constraint_list)
        return tf_theta_bounds

    def freeze(self, freeze_options=None):
        """State changing method specifing which parameters are fixed.

        During inference, you may want to freeze some parameters at
        specific values. To freeze a particular parameter, pass in a
        value for it. If you would like to freeze multiple parameters,
        you can pass in a dictionary containing multiple entries or
        call the freeze method multiple times.

        Arguments:
            freeze_options (optional): Dictionary of parameter names
                and corresponding values to be frozen (i.e., fixed)
                during inference.
        """
        # TODO freeze_options must handle nested phi.
        if freeze_options is not None:
            for param_name in freeze_options:
                if param_name is 'z':
                    z = freeze_options['z'].astype(np.float32)
                    self._check_z(z)
                    self.z['value'] = z
                    self.z['trainable'] = False
                elif param_name is 'theta':
                    for sub_param_name in freeze_options[param_name]:
                        self.theta[sub_param_name]['value'] = \
                            freeze_options[param_name][sub_param_name]
                        self.theta[sub_param_name]['trainable'] = False
                elif param_name is 'phi':
                    for sub_param_name in freeze_options[param_name]:
                        if sub_param_name is 'phi_1':
                            self._check_phi_1(
                                freeze_options['phi']['phi_1'])
                        self.phi[sub_param_name]['value'] = \
                            freeze_options[param_name][sub_param_name]
                        self.phi[sub_param_name]['trainable'] = False

    def thaw(self, thaw_options=None):
        """State changing method specifying trainable parameters.

        Complement of freeze method. If thaw_options is None, all free
        parameters are set as trainable.

        Arguments:
            thaw_options (optional): List of parameter names to set as
                trainable during inference. Valid parameter names
                include 'z', 'attention', and the parameters associated
                with the similarity kernel.
        """
        # TODO passing in phi.
        if thaw_options is None:
            self.z['trainable'] = True
            for param_name in self.theta:
                self.theta[param_name]['trainable'] = True
        else:
            for param_name in thaw_options:
                if param_name is 'z':
                    self.z['trainable'] = True
                elif param_name is 'theta':
                    for sub_param_name in thaw_options[param_name]:
                        self.theta[sub_param_name]['trainable'] = True
                elif param_name is 'phi':
                    if self.n_group is 1:
                        is_trainable = False
                    else:
                        is_trainable = True
                    for sub_param_name in thaw_options[param_name]:
                        self.phi[sub_param_name]['trainable'] = is_trainable

    def similarity(self, z_q, z_r, group_id=None, theta=None, phi=None):
        """Return similarity between two lists of points.

        Similarity is determined using the similarity kernel and the
        current similarity parameters. This method implements the
        logic for handling arguments of different shapes.

        TODO better description of the input shapes that are allowed.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim, [1, n_sample])
            z_r: A set of embedding points.
                shape = (n_trial, n_dim, [n_reference, n_sample])
            group_id (optional): The group ID for each sample. Can be a
                scalar or an array of shape = (n_trial,).
            theta (optional): The parameters governing the similarity
                kernel. If not provided, the theta associated with the
                current object is used.
            phi (optional): TODO The weights allocated to each
                dimension in a weighted minkowski metric. The weights
                should be positive and sum to the dimensionality of the
                weight vector, although this is not enforced.
                shape = (n_trial, n_dim)

        Returns:
            The corresponding similarity between rows of embedding
                points.

        """
        n_trial = z_q.shape[0]
        # Handle group_id.
        if group_id is None:
            group_id = np.zeros((n_trial), dtype=np.int32)
        else:
            if np.isscalar(group_id):
                group_id = group_id * np.ones((n_trial), dtype=np.int32)
            else:
                group_id = group_id.astype(dtype=np.int32)

        if theta is None:
            theta = self.theta
        if phi is None:
            phi = self.phi

        attention = phi['phi_1']['value'][group_id, :]

        # Make sure z_q and attention have an appropriate singleton
        # dimensions. TODO I don't like this code block.
        if z_r.ndim > 2:
            if z_q.ndim == 2:
                z_q = np.expand_dims(z_q, axis=2)
            if attention.ndim == 2:
                attention = np.expand_dims(attention, axis=2)
        if z_r.ndim == 4:
            if z_q.ndim == 3:
                z_q = np.expand_dims(z_q, axis=3)
            if attention.ndim == 3:
                attention = np.expand_dims(attention, axis=3)

        sim = self._similarity(z_q, z_r, theta, attention)
        return sim

    @abstractmethod
    def _tf_similarity(self, z_q, z_r, tf_theta, tf_attention):
        """Similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            tf_theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            tf_attention: The weights allocated to each dimension in a
                weighted minkowski metric.
                shape = (n_trial, n_dim)
        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        pass

    @abstractmethod
    def _similarity(self, z_q, z_r, theta, attention):
        """Similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            tf_theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            tf_attention: The weights allocated to each dimension in a
                weighted minkowski metric.
                shape = (n_trial, n_dim)
        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        pass

    def _get_attention(self, init_mode):
        """Return attention weights of model as TensorFlow variable."""
        if self.phi['phi_1']['trainable']:
            if init_mode is 'exact':
                tf_attention = tf.get_variable(
                    "attention", [self.n_group, self.n_dim], dtype=FLOAT_X,
                    initializer=tf.constant_initializer(
                        self.phi['phi_1']['value'], dtype=FLOAT_X
                    )
                )
            elif init_mode is 'warm':
                tf_attention = tf.get_variable(
                    "attention", [self.n_group, self.n_dim], dtype=FLOAT_X,
                    initializer=tf.constant_initializer(
                        self.phi['phi_1']['value'], dtype=FLOAT_X
                    )
                )
            else:
                n_dim = tf.constant(self.n_dim, dtype=FLOAT_X)
                alpha = tf.constant(np.ones((self.n_dim)), dtype=FLOAT_X)
                tf_attention = tf.get_variable(
                    "attention", [self.n_group, self.n_dim],
                    initializer=RandomAttention(n_dim, alpha, dtype=FLOAT_X)
                )
        else:
            tf_attention = tf.get_variable(
                "attention", [self.n_group, self.n_dim], dtype=FLOAT_X,
                initializer=tf.constant_initializer(
                    self.phi['phi_1']['value'], dtype=FLOAT_X),
                trainable=False
            )
        return tf_attention

    def _get_embedding(self, init_mode):
        """Return embedding of model as TensorFlow variable.

        Arguments:
            init_mode: A string indicating the initialization mode.
                valid options are 'cold', 'warm', and 'exact'.

        Returns:
            TensorFlow variable representing the embedding points.

        """
        # Initialize z with different scales for different restarts        

        if self.z['trainable']:
            if init_mode is 'exact':
                z = self.z['value']
                tf_z = tf.get_variable(
                    "z", [self.n_stimuli, self.n_dim], dtype=FLOAT_X,
                    initializer=tf.constant_initializer(
                        z, dtype=FLOAT_X)
                )
            elif init_mode is 'warm':
                # Mix current value with a random initialization.
                # gmm = mixture.GaussianMixture(
                #     n_components=1, covariance_type='spherical')
                # gmm.fit(self.z['value'])
                # mu = gmm.means_[0]
                # cov = gmm.covariances_[0] * np.identity(self.n_dim)
                # TODO re-work warm
                # TODO get rid init_scale_list and rand_scale_idx
                init_scale_list = [.001, .003, .01, .03, .1, .3, 1.]
                rand_scale_idx = np.random.randint(0, len(init_scale_list))
                mu = np.zeros((self.n_dim))
                cov = init_scale_list[rand_scale_idx] * np.identity(self.n_dim)
                z_noise = np.random.multivariate_normal(
                    mu, cov, (self.n_stimuli))
                # z = .8 * self.z['value'] + .2 * z_noise
                z = z_noise  # TODO
                tf_z = tf.get_variable(
                    "z", [self.n_stimuli, self.n_dim], dtype=FLOAT_X,
                    initializer=tf.constant_initializer(
                        z, dtype=FLOAT_X)
                )
            else:
                tf_z = tf.get_variable(
                    "z", [self.n_stimuli, self.n_dim], dtype=FLOAT_X,
                    initializer=RandomEmbedding(
                        mean=tf.zeros([self.n_dim], dtype=FLOAT_X),
                        stdev=tf.ones([self.n_dim], dtype=FLOAT_X),
                        minval=tf.constant(-3., dtype=FLOAT_X),
                        maxval=tf.constant(0., dtype=FLOAT_X),
                        dtype=FLOAT_X
                    )
                )
        else:
            tf_z = tf.get_variable(
                "z", [self.n_stimuli, self.n_dim], dtype=FLOAT_X,
                initializer=tf.constant_initializer(
                    self.z['value'], dtype=FLOAT_X
                ),
                trainable=False
            )
        return tf_z

    def set_log(self, do_log, log_dir=None, delete_prev=False):
        """State changing method that sets TensorBoard logging.

        Arguments:
            do_log: Boolean that indicates whether logs should be
                recorded.
            log_dir (optional): A string indicating the file path for
                the logs.
            delete_prev (optional): Boolean indicating whether the
                directory should be cleared of previous files first.
        """
        if do_log:
            self.do_log = True
            if log_dir is not None:
                self.log_dir = log_dir

        if delete_prev:
            if tf.gfile.Exists(self.log_dir):
                tf.gfile.DeleteRecursively(self.log_dir)
        tf.gfile.MakeDirs(self.log_dir)

    def fit(self, obs, n_restart=50, init_mode='cold', verbose=0):
        """Fit the free parameters of the embedding model.

        Arguments:
            obs: A Observations object representing the observed data.
            n_restart (optional): An integer specifying the number of
                restarts to use for the inference procedure. Since the
                embedding procedure can get stuck in local optima,
                multiple restarts help find the global optimum.
            init_mode (optional): A string indicating the
                initialization mode. Valid options are 'cold', 'warm',
                and 'exact'.
            verbose (optional): An integer specifying the verbosity of
                printed output. If zero, nothing is printed. Increasing
                integers display an increasing amount of information.

        Returns:
            loss_val_best: The average loss per observation on the
                validation set. Loss is defined as the negative
                loglikelihood.

        """
        #  Infer embedding.
        if (verbose > 0):
            print('Inferring embedding...')
        if (verbose > 1):
            print('    Settings:')
            print(
                '    n_stimuli: {0} | n_dim: {1} | n_group: {2}'
                ' | n_obs: {3} | n_restart: {4}'.format(
                    self.n_stimuli, self.n_dim, self.n_group,
                    obs.n_trial, n_restart))
            print('')

        # Partition observations into train and validation set to
        # control early stopping of embedding algorithm.
        skf = StratifiedKFold(n_splits=10)
        (train_idx, val_idx) = list(
            skf.split(obs.stimulus_set, obs.config_idx))[0]
        obs_train = obs.subset(train_idx)
        obs_val = obs.subset(val_idx)

        # Evaluate current model as baseline.
        loss_train_best = self.evaluate(obs_train)
        loss_val_best = self.evaluate(obs_val)
        # loss_val_best = np.inf
        z_best = self.z['value']
        attention_best = self.phi['phi_1']['value']
        theta_best = self.theta
        beat_baseline = False
        if (verbose > 2):
            print('        Baseline')
            print(
                '        '
                '     --     | loss: {0: .6f} | loss_val: {1: .6f}'.format(
                    loss_train_best, loss_val_best)
            )
            print('')

        # Initialize new model.
        (tf_loss, tf_z, tf_attention, tf_attention_constraint, tf_theta,
            tf_theta_bounds, tf_obs) = self._core_model(init_mode)

        # Bind observations.
        tf_obs_train = self._bind_obs(tf_obs, obs_train)
        tf_obs_val = self._bind_obs(tf_obs, obs_val)

        # Define optimizer op.
        tf_learning_rate = tf.placeholder(FLOAT_X, shape=[])
        # TODO Make optimizer passable argument.
        train_op = tf.train.RMSPropOptimizer(
            learning_rate=tf_learning_rate
        ).minimize(tf_loss)

        # Define summary op.
        with tf.name_scope('summaries'):
            # Create a summary to monitor loss.
            tf.summary.scalar('loss_train', tf_loss)
            # Create a summary of the embedding tensor.
            # tf.summary.tensor_summary('z', tf_z)  # Feature not supported.
            # Create a summary to monitor parameters of similarity kernel.
            with tf.name_scope('similarity'):
                for param_name in tf_theta:
                    param_mean = tf.reduce_mean(tf_theta[param_name])
                    tf.summary.scalar(param_name + '_mean', param_mean)
            summary_op = tf.summary.merge_all()

        # Run multiple restarts of embedding algorithm.
        # TODO evaluate current setting first.
        sess = tf.Session()
        for i_restart in range(n_restart):
            if (verbose > 2):
                print('        Restart {0}'.format(i_restart))
            (loss_train, loss_val, epoch, z, attention, theta) = self._fit_restart(
                sess, tf_loss, tf_z, tf_attention, tf_attention_constraint,
                tf_theta, tf_theta_bounds, tf_learning_rate, train_op,
                summary_op, tf_obs_train, tf_obs_val, i_restart, verbose
            )
            # TODO
            # gmm = mixture.GaussianMixture(
            #             n_components=1, covariance_type='spherical')
            # gmm.fit(z)
            # cov = gmm.covariances_[0]
            # print('        cov: {0:.2g}'.format(cov))

            if (verbose > 2):
                print(
                    '        '
                    'final {0:5d} | loss: {1: .6f} | loss_val: {2: .6f}'.format(
                        epoch, loss_train, loss_val)
                )
                print('')

            if loss_train < loss_train_best:
                loss_val_best = loss_val
                loss_train_best = loss_train
                z_best = z
                attention_best = attention
                theta_best = theta
                beat_baseline = True
                # print('        updated best')  # TODO
                # print('        current best | z:   {0}'.format(z[0, 0:2]))  # TODO
                # print('        current best | rho: {0}'.format(theta['rho']))  # TODO
                # print('        current best | tau: {0}'.format(theta['tau']))  # TODO

        # print('        final | z:   {0}'.format(z_best[0, 0:2]))  # TODO
        # print('        final | rho: {0}'.format(theta_best['rho']))  # TODO
        # print('        final | tau: {0}'.format(theta_best['tau']))  # TODO
        sess.close()
        tf.reset_default_graph()

        if beat_baseline:
            print('Beat baseline.')
        else:
            print('Did not beat baseline.')

        self.z['value'] = z_best
        self.phi['phi_1']['value'] = attention_best
        self._set_theta(theta_best)

        return loss_train_best  # TODO also return loss_train_best

    def _fit_restart(
            self, sess, tf_loss, tf_z, tf_attention, tf_attention_constraint,
            tf_theta, tf_theta_bounds, tf_learning_rate, train_op, summary_op,
            tf_obs_train, tf_obs_val, i_restart, verbose):
        """Embed using a TensorFlow implementation."""
        sess.run(tf.global_variables_initializer())

        # Write logs for TensorBoard
        if self.do_log:
            summary_writer = tf.summary.FileWriter(
                '%s/%s' % (self.log_dir, i_restart),
                graph=tf.get_default_graph()
            )

        loss_train_best = np.inf
        loss_val_best = np.inf

        lr = copy.copy(self.lr)

        last_improvement_stop = 0
        last_improvement_reduce = 0
        # n_batch = int(np.ceil(obs_train.n_trial / self.batch_size))
        # batch_idx = np.repeat(
        #     np.expand_dims(np.arange(n_batch, dtype=np.int), axis=1),
        #     self.batch_size, axis=1
        # )
        # batch_idx = batch_idx.flatten()
        # batch_idx = batch_idx[0:obs_train.n_trial]
        # for epoch in range(self.max_n_epoch):
        #     np.random.shuffle(batch_idx)
        #     loss_train_batch = np.empty(n_batch)
        #     for i_batch in range(n_batch):
        #         obs_train_batch = obs_train.subset(np.equal(batch_idx, i_batch))
        #         _, loss_train_batch[i_batch], summary = sess.run(
        #             [train_op, tf_loss, summary_op],
        #             feed_dict={
        #                 tf_learning_rate: lr,
        #                 **self._bind_obs(tf_obs, obs_train_batch)
        #             }
        #         )
        #         sess.run(tf_theta_bounds)
        #         sess.run(tf_attention_constraint)
        #     loss_train = np.mean(loss_train_batch)

        for epoch in range(self.max_n_epoch):
            _, loss_train, summary = sess.run(
                [train_op, tf_loss, summary_op],
                feed_dict={tf_learning_rate: lr, **tf_obs_train}
            )
            sess.run(tf_theta_bounds)
            sess.run(tf_attention_constraint)
            loss_val = sess.run(
                tf_loss, feed_dict=tf_obs_val
            )

            # Compare current loss to best loss.
            if loss_val < loss_val_best:
                last_improvement_stop = 0
            else:
                last_improvement_stop = last_improvement_stop + 1

            if loss_val < loss_val_best:
                last_improvement_reduce = 0
            else:
                last_improvement_reduce = last_improvement_reduce + 1

            if loss_val < loss_val_best:
                loss_train_best = loss_train
                loss_val_best = loss_val
                epoch_best = epoch + 1
                (z_best, attention_best) = sess.run(
                    [tf_z, tf_attention])
                theta_best = {}
                for param_name in tf_theta:
                    theta_best[param_name] = {}
                    theta_best[param_name]['value'] = sess.run(tf_theta[param_name])[0]

            if last_improvement_stop >= self.patience_stop:
                break
            # if last_improvement_reduce >= self.patience_reduce:
            #     lr = .9 * lr
            #     lr = np.maximum(lr, 1e-10)
            #     last_improvement_reduce = 0

            if not epoch % 10:
                # Write logs at every 10th iteration
                if self.do_log:
                    summary_writer.add_summary(summary, epoch)
            if verbose > 3:
                print(
                    "        epoch {0:5d} | ".format(epoch),
                    "loss: {0: .6f} | ".format(loss_train),
                    "loss_val: {0: .6f}".format(loss_val)
                )

        # Handle pathological case where there is no improvement.
        if z_best is None:
            epoch_best = epoch + 1
            z_best = self.z['value']
            attention_best = self.phi['phi_1']['value']
            theta_best = self.theta

        # TODO loss_all ?
        return (
            loss_train_best, loss_val_best, epoch_best, z_best, attention_best,
            theta_best)

    def evaluate(self, obs):
        """Evaluate observations using the current state of the model.

        Arguments:
            obs: A Observations object representing the observed data.

        Returns:
            loss: The average loss per observation. Loss is defined as
                the negative loglikelihood.

        """
        (tf_loss, _, _, _, _, _, tf_obs) = self._core_model('exact')

        init = tf.global_variables_initializer()
        sess = tf.Session()
        sess.run(init)
        loss = sess.run(tf_loss, feed_dict=self._bind_obs(tf_obs, obs))

        sess.close()
        tf.reset_default_graph()
        return loss

    def _bind_obs(self, tf_obs, obs):
        feed_dict = {
            tf_obs['stimulus_set']: obs.stimulus_set,
            tf_obs['n_reference']: obs.n_reference,
            tf_obs['n_select']: obs.n_select,
            tf_obs['is_ranked']: obs.is_ranked,
            tf_obs['group_id']: obs.group_id,
            tf_obs['config_idx']: obs.config_idx,
            tf_obs['n_config']: len(obs.config_list.n_outcome.values),
            tf_obs['max_n_outcome']: np.max(obs.config_list.n_outcome.values),
            tf_obs['config_n_reference']: obs.config_list.n_reference.values,
            tf_obs['config_n_select']: obs.config_list.n_select.values,
            tf_obs['config_is_ranked']: obs.config_list.is_ranked.values,
            tf_obs['config_n_outcome']: obs.config_list.n_outcome.values,
            tf_obs['config_outcome_tensor']: obs.outcome_tensor()
        }
        return feed_dict

    def _project_attention(self, tf_attention_0):
        """Return projection of attention weights."""
        n_dim = tf.shape(tf_attention_0, out_type=FLOAT_X)[1]
        tf_attention_1 = tf.divide(
            tf.reduce_sum(tf_attention_0, axis=1, keepdims=True), n_dim
        )
        tf_attention_proj = tf.divide(
            tf_attention_0, tf_attention_1
        )

        return tf_attention_proj

    def _core_model(self, init_mode):
        """Embedding model implemented using TensorFlow."""
        with tf.variable_scope("model"):
            # Free parameters.
            tf_theta = self._get_similarity_parameters(init_mode)
            tf_theta_bounds = self._get_similarity_constraints(tf_theta)
            tf_attention = self._get_attention(init_mode)
            tf_z = self._get_embedding(init_mode)

            # Observation data.
            tf_stimulus_set = tf.placeholder(
                tf.int32, [None, None], name='stimulus_set'
            )
            tf_n_reference = tf.placeholder(
                tf.int32, [None], name='n_reference')
            tf_n_select = tf.placeholder(
                tf.int32, [None], name='n_select')
            tf_is_ranked = tf.placeholder(
                tf.int32, [None], name='is_ranked')
            tf_group_id = tf.placeholder(
                tf.int32, [None], name='group_id')
            tf_config_idx = tf.placeholder(
                tf.int32, [None], name='config_idx')
            # Configuration data.
            tf_n_config = tf.placeholder(
                tf.int32, shape=(), name='n_config'
            )
            tf_max_n_outcome = tf.placeholder(
                tf.int32, shape=(), name='max_n_outcome'
            )
            tf_config_n_reference = tf.placeholder(
                tf.int32, [None], name='config_n_reference')
            tf_config_n_select = tf.placeholder(
                tf.int32, [None], name='config_n_select')
            tf_config_is_ranked = tf.placeholder(
                tf.int32, [None], name='config_is_ranked')
            tf_config_n_outcome = tf.placeholder(
                tf.int32, [None], name='config_n_outcome')
            tf_outcome_tensor = tf.placeholder(
                tf.int32, name='config_outcome_tensor')
            tf_obs = {
                'stimulus_set': tf_stimulus_set,
                'n_reference': tf_n_reference,
                'n_select': tf_n_select,
                'is_ranked': tf_is_ranked,
                'group_id': tf_group_id,
                'config_idx': tf_config_idx,
                'n_config': tf_n_config,
                'max_n_outcome': tf_max_n_outcome,
                'config_n_reference': tf_config_n_reference,
                'config_n_select': tf_config_n_select,
                'config_is_ranked': tf_config_is_ranked,
                'config_n_outcome': tf_config_n_outcome,
                'config_outcome_tensor': tf_outcome_tensor
            }

            n_trial = tf.shape(tf_stimulus_set)[0]
            max_n_reference = tf.shape(tf_stimulus_set)[1] - 1

            # Expand attention weights.
            tf_atten_expanded = tf.gather(tf_attention, tf_group_id)

            # Compute similarity between query and references.
            (z_q, z_r) = self._tf_inflate_points(
                tf_stimulus_set, max_n_reference, tf_z)
            sim_qr = self._tf_similarity(
                z_q, z_r, tf_theta, tf.expand_dims(tf_atten_expanded, axis=2)
            )
            # sim_qr_var = tf.Variable(sim_qr, trainable=False, name="sim_qr_var")  # TODO try, use_resource=True

            # Compute the probability of observations for the different trial
            # configurations.
            prob_all = tf.zeros((0), dtype=FLOAT_X)
            cond_idx = tf.constant(0, dtype=tf.int32)

            def cond_fn(cond_idx, prob_all):
                return tf.less(cond_idx, tf_n_config)

            def body_fn(cond_idx, prob_all):
                n_reference = tf_n_reference[cond_idx]
                n_select = tf_n_select[cond_idx]
                trial_idx = tf.squeeze(tf.where(tf.logical_and(
                    tf.equal(tf_n_reference, n_reference),
                    tf.equal(tf_n_select, n_select)))
                )
                sim_qr_config = tf.gather(sim_qr, trial_idx)
                reference_idx = tf.range(
                    0, n_reference, delta=1, dtype=tf.int32)
                sim_qr_config = tf.gather(sim_qr_config, reference_idx, axis=1)
                sim_qr_config.set_shape((None, None))
                prob_config = self._tf_ranked_sequence_probability(
                    sim_qr_config, n_select)
                prob_all = tf.concat((prob_all, prob_config), axis=0)
                cond_idx = cond_idx + 1
                return [cond_idx, prob_all]

            r = tf.while_loop(
                cond_fn, body_fn,
                loop_vars=[cond_idx, prob_all],
                shape_invariants=[cond_idx.get_shape(), tf.TensorShape([None])]
            )
            prob_all = r[1]

            # Cost function
            n_trial = tf.cast(n_trial, dtype=FLOAT_X)
            cap = tf.constant(2.2204e-16, dtype=FLOAT_X)
            loss = tf.negative(
                tf.reduce_sum(tf.log(tf.maximum(prob_all, cap))))
            loss = tf.divide(loss, n_trial)

            tf_attention_constraint = tf_attention.assign(
                self._project_attention(tf_attention))

        return (
            loss, tf_z, tf_attention, tf_attention_constraint, tf_theta,
            tf_theta_bounds, tf_obs
        )

    def log_likelihood(self, obs, z=None, theta=None, phi=None):
        """Return the log likelihood of a set of observations.

        Arguments:
            obs: A set of judged similarity trials. The indices
                used must correspond to the rows of z.
            z (optional): The z parameters.
            theta (optional): The theta parameters.
            phi (optional): The phi parameters.

        Returns:
            The total log-likelihood of the observations.

        Notes:
            The arguments z, theta, and phi are assigned in a lazy
                manner. If they have a value of None when they are
                needed, the attribute values of the object will be used.

        """
        cap = 2.2204e-16
        prob_all = self.outcome_probability(
            obs, group_id=obs.group_id, z=z, theta=theta, phi=phi,
            unaltered_only=True)
        prob = ma.maximum(cap, prob_all[:, 0])
        ll = ma.sum(ma.log(prob))
        return ll

    def outcome_probability(
            self, docket, group_id=None, z=None, theta=None, phi=None,
            unaltered_only=False):
        """Return probability of each outcome for each trial.

        Arguments:
            docket: A docket of unjudged similarity trials. The indices
                used must correspond to the rows of z.
            group_id (optional): The group ID for which to compute the
                probabilities.
            z (optional): A set of embedding points. If no embedding
                points are provided, the points associated with the
                object are used.
                shape=(n_stimuli, n_dim, [n_sample])
            theta (optional): The theta parameters.
            phi (optionsl): The phi parameters.
            unaltered_only (optional): Flag the determines whether only
                the unaltered ordering is evaluated.

        Returns:
            prob_all: A MaskedArray representing the probabilities
                associated with the different outcomes for each
                unjudged trial. In general, different trial
                configurations have a different number of possible
                outcomes. The mask attribute of the MaskedArray
                indicates which elements are actual outcome
                probabilities.
                shape = (n_trial, n_max_outcome, [n_sample])

        Notes:
            The first outcome corresponds to the original order of the
                trial data.
            The arguments theta and phi are assigned in a lazy
                manner. If they have a value of None when they are
                needed, the attribute values of the object will be used.

        """
        n_trial_all = docket.n_trial

        if z is None:
            z = self.z['value']
        else:
            self._check_z(z)

        n_config = docket.config_list.shape[0]

        outcome_idx_list = docket.outcome_idx_list
        n_outcome_list = docket.config_list['n_outcome'].values
        max_n_outcome = np.max(n_outcome_list)
        # ==================================================
        # Create an analogous tensor.
        # shape = (n_config, max_n_outcome, max_n_ref)
        # n_reference_list = docket.config_list['n_reference'].values
        # max_n_reference = np.max(n_reference_list)
        # outcome_tensor = docket.outcome_tensor()
        # ==================================================
        if unaltered_only:
            max_n_outcome = 1

        if z.ndim == 2:
            z = np.expand_dims(z, axis=2)
        n_sample = z.shape[2]

        # Compute similarity between query and references.
        (z_q, z_r) = self._inflate_points(
            docket.stimulus_set, docket.max_n_reference, z)

        sim_qr = self.similarity(
            z_q, z_r, group_id=group_id, theta=theta, phi=phi)

        # TODO generalize remaining code for multiple samples.
        # sim_qr = sim_qr[:, :, 0] TODO
        prob_all = -1 * np.ones((n_trial_all, max_n_outcome, n_sample))
        for i_config in range(n_config):
            config = docket.config_list.iloc[i_config]
            outcome_idx = outcome_idx_list[i_config]
            # outcome_idx = outcome_tensor[
            #     i_config,
            #     0:n_outcome_list[i_config],
            #     0:n_reference_list[i_config]
            # ]
            trial_locs = docket.config_idx == i_config
            n_trial = np.sum(trial_locs)
            n_reference = config['n_reference']

            sim_qr_config = sim_qr[trial_locs]
            sim_qr_config = sim_qr_config[:, 0:n_reference]

            n_outcome = n_outcome_list[i_config]
            if unaltered_only:
                n_outcome = 1

            # Compute probability of each possible outcome.
            prob = np.ones((n_trial, n_outcome, n_sample), dtype=np.float64)
            for i_outcome in range(n_outcome):
                s_qref_perm = sim_qr_config[:, outcome_idx[i_outcome, :], :]
                prob[:, i_outcome, :] = self._ranked_sequence_probabiltiy(
                    s_qref_perm, config['n_select'])
            prob_all[trial_locs, 0:n_outcome, :] = prob
        prob_all = ma.masked_values(prob_all, -1)

        # Correct for numerical inaccuracy.
        if not unaltered_only:
            prob_all = ma.divide(
                prob_all, ma.sum(prob_all, axis=1, keepdims=True))
        if n_sample == 1:
            prob_all = prob_all[:, :, 0]
        return prob_all

    def _tf_outcome_probability(self, docket, z_tf, tf_theta):
        """Return probability of outcomes for each trial.

        Arguments:
            docket: A docket of unjudged similarity trials.
            z_tf: TensorFlow tensor representing embedding points.
            tf_theta: Dictionary of Tensorflow tensors representing
                free parameters of similarity kernel.

        Returns:
            outcome_idx_list: A list with one entry for each display
                configuration. Each entry contains a 2D array where
                each row contains the indices describing one outcome.
            prob_all: The probabilities associated with the different
                outcomes for each unjudged trial. In general, different
                trial configurations will have a different number of
                possible outcomes. Trials with a smaller number of
                possible outcomes are element padded with zeros to
                match the trial with the maximum number of possible
                outcomes.

        """
        n_trial_all = docket.n_trial
        dmy_idx = np.arange(n_trial_all)
        n_config = docket.config_list.shape[0]

        stimulus_set = tf.constant(docket.stimulus_set, dtype=tf.int32)
        max_n_reference = stimulus_set.get_shape()[1] - 1

        attention = self.phi['phi_1']['value'][0, :]  # TODO HACK
        attention = np.expand_dims(attention, axis=0)
        attention = np.expand_dims(attention, axis=2)
        tf_attention = tf.convert_to_tensor(
            attention, dtype=FLOAT_X
        )

        # Compute similarity between query and references.
        (z_q, z_r) = self._tf_inflate_points(
            stimulus_set, max_n_reference, z_tf)
        sim_qr = self._tf_similarity(z_q, z_r, tf_theta, tf_attention)

        outcome_idx_list = docket.outcome_idx_list
        n_outcome_list = docket.config_list['n_outcome'].values
        max_n_outcome = np.max(n_outcome_list)

        prob_all = tf.zeros((0, max_n_outcome), dtype=FLOAT_X)
        indices_all = tf.zeros((0), dtype=tf.int32)
        for i_config in range(n_config):
            config = docket.config_list.iloc[i_config]
            outcome_idx = outcome_idx_list[i_config]
            trial_locs = docket.config_idx == i_config
            n_trial = np.sum(trial_locs)
            n_outcome = n_outcome_list[i_config]
            n_reference = tf.constant(config['n_reference'], dtype=tf.int32)
            n_select = tf.constant(config['n_select'], dtype=tf.int32)

            curr_trial_idx = tf.constant(dmy_idx[trial_locs], dtype=tf.int32)
            sim_qr_config = tf.gather(sim_qr, curr_trial_idx)
            reference_idx = tf.range(0, n_reference, delta=1, dtype=tf.int32)
            sim_qr_config = tf.gather(sim_qr_config, reference_idx, axis=1)

            # Compute probability of each possible outcome.
            prob = tf.ones((0, n_trial), dtype=np.float32)
            for i_outcome in range(n_outcome):
                s_qref_perm = tf.gather(
                    sim_qr_config, outcome_idx[i_outcome, :], axis=1)
                prob = tf.concat((
                    prob,
                    tf.expand_dims(self._tf_ranked_sequence_probability(
                        s_qref_perm, n_select), axis=0)
                ), axis=0)
            # Pad absent outcomes before putting in master prob_all.
            prob_zero = tf.zeros((max_n_outcome - n_outcome, n_trial))
            prob = tf.concat((prob, prob_zero), axis=0)
            prob = tf.transpose(prob)
            indices = dmy_idx[trial_locs]
            indices_all = tf.concat((indices_all, indices), axis=0)
            prob_all = tf.concat((prob_all, prob), axis=0)
        prob_all = tf.gather(prob_all, indices_all)

        # Correct for numerical inaccuracy.
        prob_all = tf.divide(
            prob_all, tf.reduce_sum(prob_all, axis=1, keepdims=True))
        return prob_all

    def _inflate_points(self, stimulus_set, n_reference, z):
        """Inflate stimulus set into embedding points.

        Arguments:
            stimulus_set: Array of integers indicating the stimuli used
                in each trial.
                shape = (n_trial, >= (n_reference + 1))
            n_reference: A scalar indicating the number of references
                in each trial.
            z: shape = (n_stimuli, n_dim, n_sample)

        Returns:
            z_q: shape = (n_trial, n_dim, 1, n_sample)
            z_r: shape = (n_trial, n_dim, n_reference, n_sample)

        """
        n_trial = stimulus_set.shape[0]
        n_dim = z.shape[1]
        n_sample = z.shape[2]

        stimulus_set_temp = copy.copy(stimulus_set) + 1
        z_placeholder = np.zeros((1, n_dim, n_sample), dtype=np.float32)
        z_temp = np.concatenate((z_placeholder, z), axis=0)

        # Inflate query stimuli.
        z_q = z_temp[stimulus_set_temp[:, 0], :, :]
        z_q = np.expand_dims(z_q, axis=2)
        # Inflate reference stimuli.
        z_r = np.empty((n_trial, n_dim, n_reference, n_sample))
        for i_ref in range(n_reference):
            z_r[:, :, i_ref, :] = z_temp[stimulus_set_temp[:, 1+i_ref], :, :]
        return (z_q, z_r)

    def _tf_inflate_points(
            self, stimulus_set, n_reference, z):
        """Inflate stimulus set into embedding points."""
        n_trial = tf.shape(stimulus_set)[0]

        n_dim = tf.shape(z)[1]

        stimulus_set_temp = stimulus_set + 1
        z_placeholder = tf.zeros((1, n_dim), dtype=FLOAT_X)
        z_temp = tf.concat((z_placeholder, z), axis=0)

        # Inflate query stimuli.
        z_q = tf.gather(z_temp, stimulus_set_temp[:, 0])
        z_q = tf.expand_dims(z_q, axis=2)

        # Initialize z_r.
        i_ref = tf.constant(0, dtype=tf.int32)
        z_r = tf.ones([n_trial, n_dim, n_reference], dtype=FLOAT_X)

        def cond_fn(i_ref, z_temp, stimulus_set_temp, z_r):
            return tf.less(i_ref, n_reference)

        def body_fn(i_ref, z_temp, stimulus_set_temp, z_r):
            z_r_new = tf.gather(z_temp, stimulus_set_temp[:, 1+i_ref])
            z_r_new = tf.expand_dims(z_r_new, axis=2)
            z_r = tf.concat(
                [z_r[:, :, :i_ref], z_r_new, z_r[:, :, i_ref+1:]],
                axis=2
            )
            i_ref = i_ref + 1
            return [i_ref, z_temp, stimulus_set_temp, z_r]

        r = tf.while_loop(
            cond_fn, body_fn, [i_ref, z_temp, stimulus_set_temp, z_r],
            [
                i_ref.get_shape(), z_temp.get_shape(),
                stimulus_set_temp.get_shape(),
                tf.TensorShape([None, None, None])
            ]
        )
        z_r = r[3]
        return (z_q, z_r)

    def _ranked_sequence_probabiltiy(self, sim_qr, n_select):
        """Return probability of a ranked selection sequence.

        Arguments:
            sim_qr: A 3D tensor containing pairwise similarity values.
                Each row (dimension 0) contains the similarity between
                a trial's query stimulus and reference stimuli. The
                tensor is arranged such that the first column
                corresponds to the first selection in a sequence, and
                the last column corresponds to the last selection
                (dimension 1). The third dimension indicates
                different samples.
                shape = (n_trial, n_reference, n_sample)
            n_select: Scalar indicating the number of selections made
                by an agent.

        Returns:
            A 2D tensor of probabilities.
            shape = (n_trial, n_sample)

        Notes:
            For example, given query Q, the probability of selecting
            the references A, B, and C (in that order) would be:

            P(A,B,C) = s_QA/(s_QA + s_QB + s_QC) * s_QB/(s_QB + s_QC)

            where s_QA denotes the similarity between they query and
            reference A.

            The probability is computed by starting with the last
            selection for efficiency and numerical stability. In the
            provided example, this corresponds to first computing the
            probability of selecting B second, given that A was
            selected first.

        """
        n_trial = sim_qr.shape[0]
        n_sample = sim_qr.shape[2]

        # Initialize.
        seq_prob = np.ones((n_trial, n_sample), dtype=np.float64)
        selected_idx = n_select - 1
        denom = np.sum(sim_qr[:, selected_idx:, :], axis=1)  # TODO faster to sum over first dimension?

        for i_selected in range(selected_idx, -1, -1):
            # Compute selection probability.
            prob = np.divide(sim_qr[:, i_selected], denom)
            # Update sequence probability.
            seq_prob = np.multiply(seq_prob, prob)
            # Update denominator in preparation for computing the probability
            # of the previous selection in the sequence.
            if i_selected > 0:
                denom = denom + sim_qr[:, i_selected-1, :]
        return seq_prob

    def _tf_ranked_sequence_probability(self, sim_qr, n_select):
        """Return probability of a ranked selection sequence.

        See: _ranked_sequence_probability

        Arguments:
            sim_qr:
            n_select:

        TODO complete docs, MAYBE implement samples dimension?
        """
        n_trial = tf.shape(sim_qr)[0]
        # n_trial = tf.cast(n_trial, dtype=tf.int32)

        # Initialize.
        seq_prob = tf.ones([n_trial], dtype=FLOAT_X)
        selected_idx = n_select - 1
        denom = tf.reduce_sum(sim_qr[:, selected_idx:], axis=1)

        def cond_fn(selected_idx, seq_prob, denom):
            return tf.greater_equal(
                selected_idx, tf.constant(0, dtype=tf.int32))

        def body_fn(selected_idx, seq_prob, denom):
            # Compute selection probability.
            prob = tf.divide(sim_qr[:, selected_idx], denom)
            # Update sequence probability.
            seq_prob = np.multiply(seq_prob, prob)
            # Update denominator in preparation for computing the
            # probability of the previous selection in the sequence.
            denom = tf.cond(
                selected_idx > tf.constant(0, dtype=tf.int32),
                lambda: tf.add(denom, sim_qr[:, selected_idx - 1]),
                lambda: denom,
                name='increase_denom'
            )
            return [selected_idx - 1, seq_prob, denom]

        r = tf.while_loop(
            cond_fn, body_fn, [selected_idx, seq_prob, denom]
        )
        return r[1]

    def posterior_samples(
            self, obs, n_final_sample=1000, n_burn=100, thin_step=5,
            z_init=None, verbose=0):
        """Sample from the posterior of the embedding.

        Samples are drawn from the posterior holding theta constant. A
        variant of Eliptical Slice Sampling (Murray & Adams 2010) is
        used to estimate the posterior for the embedding points. Since
        the latent embedding variables are translation and rotation
        invariant, generic sampling will artificailly inflate the
        entropy of the samples. To compensate for this issue, N
        embedding points are selected to serve as anchor points, where
        N is two times the dimensionality of the embedding. Two chains
        are run each using half of the anchor points. The samples from
        the two chains are merged in order to get a posterior estimate
        for all points.

        Arguments:
            obs: A Observations object representing the observed data.
            n_final_sample (optional): The number of samples desired
                after removing the "burn in" samples and applying
                thinning.
            n_burn (optional): The number of samples to remove from the
                beginning of the sampling sequence.
            thin_step (optional): The interval to use in order to thin
                (i.e., de-correlate) the samples.
            z_init (optional): Initialization of z. If not provided,
                the current embedding values associated with the object
                are used.
            verbose (optional): An integer specifying the verbosity of
                printed output. If zero, nothing is printed. Increasing
                integers display an increasing amount of information.

        Returns:
            A dictionary of posterior samples for different parameters.
                The samples are stored as a NumPy array.
                'z' : shape = (n_stimuli, n_dim, n_total_sample).

        Notes:
            The step_size of the Hamiltonian Monte Carlo procedure is
                determined by the scale of the current embedding.

        References:
            Murray, I., & Adams, R. P. (2010). Slice sampling
            covariance hyperparameters of latent Gaussian models. In
            Advances in Neural Information Processing Systems (pp.
            1732-1740).

        """
        n_final_sample = int(n_final_sample)
        n_total_sample = n_burn + (n_final_sample * thin_step)
        n_stimuli = self.n_stimuli
        n_dim = self.n_dim
        if z_init is None:
            z = copy.copy(self.z['value'])
        else:
            z = z_init

        if verbose > 0:
            print('Sampling from posterior...')
        if (verbose > 1):
            print('    Settings:')
            print('    n_total_sample: ', n_total_sample)
            print('    n_burn:         ', n_burn)
            print('    thin_step:      ', thin_step)
            print('    --------------------------')
            print('    n_final_sample: ', n_final_sample)

        # Prior
        # p(z_k | Z_negk, theta) ~ N(mu, sigma)
        # Approximate prior of z_k using all embedding points to reduce
        # computational burden.
        gmm = mixture.GaussianMixture(
            n_components=1, covariance_type='spherical')
        gmm.fit(z)
        mu = gmm.means_[0]  # TODO verify was mu = gmm.means_ without [0]
        sigma = gmm.covariances_[0] * np.identity(n_dim)

        # Center embedding to satisfy assumptions of elliptical slice sampling.
        z = z - mu

        n_partition = 2

        # Define log-likelihood for elliptical slice sampler.
        def flat_log_likelihood(z_part, part_idx, z_full, obs):
            # Assemble full z.
            n_stim_part = np.sum(part_idx)
            z_full[part_idx, :] = np.reshape(
                z_part, (n_stim_part, n_dim), order='C')
            return self.log_likelihood(obs, z=z_full)  # TODO pass in phi

        # Initalize sampler.
        z_full = copy.copy(z)
        samples = np.empty((n_stimuli, n_dim, n_total_sample))

        # # Sample from prior if there are no observations. TODO
        # if obs.n_trial is 0:
        #     z = np.random.multivariate_normal(
        #         np.zeros((n_dim)), sigma, n_stimuli)
        # else:

        for i_round in range(n_total_sample):
            # Partition stimuli into two groups.
            if np.mod(i_round, 100) == 0:
                part_idx, n_stimuli_part = self._make_partition(n_stimuli, n_partition)
                # Create a diagonally tiled covariance matrix in order to slice
                # multiple points simultaneously.
                prior = []
                for i_part in range(n_partition):
                    prior.append(
                        np.linalg.cholesky(
                            self._inflate_sigma(sigma, n_stimuli_part[i_part], n_dim))
                    )
            # print('\r{0}'.format(i_round)) # TODO
            for i_part in range(n_partition):
                z_part = z_full[part_idx[i_part], :]
                z_part = z_part.flatten('C')

                (z_part, _) = elliptical_slice(
                    z_part, prior[i_part], flat_log_likelihood,
                    pdf_params=[part_idx[i_part], copy.copy(z), obs])

                z_part = np.reshape(
                    z_part, (n_stimuli_part[i_part], n_dim), order='C')
                # Update z_full.
                z_full[part_idx[i_part], :] = z_part
            samples[:, :, i_round] = z_full

        # Add back in mean.
        mu = np.expand_dims(mu, axis=2)
        samples = samples + mu

        samples_all = samples[:, :, n_burn::thin_step]
        samples_all = samples_all[:, :, 0:n_final_sample]
        samples = dict(z=samples_all)
        return samples

    def _make_partition(self, n_stimuli, n_partition):
        """Partition stimuli.
        
        Arguments:
            n_stimuli: Scalar indicating the total number of stimuli.
            n_partition: Scalar indicating the number of partitions.
        
        Returns:
            part_idx: A boolean array indicating partition membership.
                shape = (n_partition, n_stimuli)
            n_stimuli_part: An integer array indicating the number of
                stimuli in each partition.
                shape = (n_partition)

        """
        n_stimuli_part = np.floor(n_stimuli / n_partition)
        n_stimuli_part = n_stimuli_part * np.ones([n_partition])
        n_stimuli_part[1] = n_stimuli_part[1] + (
            n_stimuli - (n_stimuli_part[1] * n_partition)
        )
        n_stimuli_part = n_stimuli_part.astype(np.int32)

        partition = np.empty([0])
        for i_part in range(n_partition):
            partition = np.hstack(
                (partition, i_part * np.ones([n_stimuli_part[i_part]]))
            )
        partition = np.random.choice(partition, n_stimuli, replace=False)

        part_idx = np.zeros((n_partition, n_stimuli), dtype=np.int32)
        for i_part in range(n_partition):
            locs = np.equal(partition, i_part)
            part_idx[i_part, locs] = 1
        part_idx = part_idx.astype(bool)

        return part_idx, n_stimuli_part

    # def posterior_samples(
    #         self, obs, n_final_sample=1000, n_burn=1000, thin_step=3, verbose=0):
    #     """Sample from the posterior of the embedding.

    #     Samples are drawn from the posterior holding theta constant. A
    #     variant of Eliptical Slice Sampling (Murray & Adams 2010) is
    #     used to estimate the posterior for the embedding points. Since
    #     the latent embedding variables are translation and rotation
    #     invariant, generic sampling will artificailly inflate the
    #     entropy of the samples. To compensate for this issue, N
    #     embedding points are selected to serve as anchor points, where
    #     N is two times the dimensionality of the embedding. Two chains
    #     are run each using half of the anchor points. The samples from
    #     the two chains are merged in order to get a posterior estimate
    #     for all points.

    #     Arguments:
    #         obs: A Observations object representing the observed data.
    #         n_final_sample (optional): The number of samples desired after
    #             removing the "burn in" samples and applying thinning.
    #         n_burn (optional): The number of samples to remove from the
    #             beginning of the sampling sequence.
    #         thin_step (optional): The interval to use in order to thin
    #             (i.e., de-correlate) the samples.
    #         verbose (optional): An integer specifying the verbosity of
    #             printed output. If zero, nothing is printed. Increasing
    #             integers display an increasing amount of information.

    #     Returns:
    #         A dictionary of posterior samples for different parameters.
    #             The samples are stored as a NumPy array.
    #             'z' : shape = (n_final_sample, n_stimuli, n_dim).

    #     Notes:
    #         The step_size of the Hamiltonian Monte Carlo procedure is
    #             determined by the scale of the current embedding.

    #     References:
    #         Murray, I., & Adams, R. P. (2010). Slice sampling
    #         covariance hyperparameters of latent Gaussian models. In
    #         Advances in Neural Information Processing Systems (pp.
    #         1732-1740).

    #     """
    #     n_sample_set = np.ceil(n_final_sample/2).astype(np.int32)
    #     n_stimuli = self.n_stimuli
    #     n_dim = self.n_dim
    #     z = copy.copy(self.z['value'])
    #     n_anchor_point = n_dim

    #     if verbose > 0:
    #         print('Sampling from posterior...')
    #     if (verbose > 1):
    #         print('    Settings:')
    #         print('    n_final_sample: ', n_final_sample)
    #         print('    n_burn: ', n_burn)
    #         print('    thin_step: ', thin_step)

    #     # Prior
    #     # p(z_k | Z_negk, theta) ~ N(mu, sigma)
    #     # Approximate prior of z_k using all embedding points to reduce
    #     # computational burden.
    #     gmm = mixture.GaussianMixture(
    #         n_components=1, covariance_type='spherical')
    #     gmm.fit(z)
    #     mu = gmm.means_[0]  # TODO verify was mu = gmm.means_ without [0]
    #     sigma = gmm.covariances_[0] * np.identity(n_dim)

    #     # Center embedding to satisfy assumptions of elliptical slice sampling.
    #     z = z - mu
    #     mu = np.expand_dims(mu, axis=2)

    #     # Create a diagonally tiled covariance matrix in order to slice
    #     # multiple points simultaneously.
    #     prior = np.linalg.cholesky(
    #         self._inflate_sigma(sigma, n_stimuli - n_anchor_point, n_dim))

    #     # Select anchor points.
    #     anchor_idx = self._select_anchor_points(z, n_anchor_point)
    #     sample_idx = self._create_sample_idx(n_stimuli, anchor_idx)

    #     # Define log-likelihood for elliptical slice sampler.
    #     def flat_log_likelihood(z_samp, sample_idx, z_full, obs):
    #         # Assemble full z.
    #         n_samp_stimuli = len(sample_idx)
    #         z_full[sample_idx, :] = np.reshape(
    #             z_samp, (n_samp_stimuli, n_dim), order='C')
    #         return self.log_likelihood(obs, z=z_full)  # TODO pass in phi

    #     combined_samples = [None, None]
    #     for i_set in range(2):

    #         # Initalize sampler.
    #         n_total_sample = n_burn + (n_sample_set * thin_step)
    #         z_samp = z[sample_idx[:, i_set], :]
    #         z_samp = z_samp.flatten('C')
    #         samples = np.empty((n_stimuli, n_dim, n_total_sample))

    #         # Sample from prior if there are no observations. TODO
    #         # if obs.n_trial is 0:
    #         #     z = np.random.multivariate_normal(
    #         #         np.zeros((n_dim)), sigma, n_stimuli)

    #         for i_round in range(n_total_sample):
    #             # print('\r{0}'.format(i_round)) # TODO
    #             # TODO should pdf params allow for keyword arguments?
    #             (z_samp, _) = elliptical_slice(
    #                 z_samp, prior, flat_log_likelihood,
    #                 pdf_params=[sample_idx[:, i_set], copy.copy(z), obs])
    #             # Merge z_samp_r and z_anchor
    #             z_full = copy.copy(z)
    #             z_samp_r = np.reshape(
    #                 z_samp, (n_stimuli - n_anchor_point, n_dim), order='C')
    #             z_full[sample_idx[:, i_set], :] = z_samp_r
    #             samples[:, :, i_round] = z_full

    #         # Add back in mean.
    #         samples = samples + mu
    #         combined_samples[i_set] = samples[:, :, n_burn::thin_step]

    #     # Replace anchors with samples from other set.
    #     for i_point in range(n_anchor_point):
    #         combined_samples[0][anchor_idx[i_point, 0], :, :] = \
    #             combined_samples[1][anchor_idx[i_point, 0], :, :]
    #     for i_point in range(n_anchor_point):
    #         combined_samples[1][anchor_idx[i_point, 1], :, :] = \
    #             combined_samples[0][anchor_idx[i_point, 1], :, :]

    #     samples_all = np.concatenate(
    #         (combined_samples[0], combined_samples[1]), axis=2
    #     )
    #     samples_all = samples_all[:, :, 0:n_final_sample]
    #     samples = dict(z=samples_all)
    #     return samples

    def _select_anchor_points(self, z, n_point):
        """Select anchor points for posterior inference.

        Anchor points are selected so that the sets are
        non-overlapping, all points are far from the center of the
        distribution, all points are far from each other, all points
        within a set are far from one another.

        This code assumes that the incoming points have already been
        centered.

        Arguments:
            z: The embedding points.
            n_point: The number of points in each set.

        Returns:
            anchor_idx

        """
        n_set = 2
        n_stimuli = z.shape[0]
        n_point_total = n_point * n_set

        # if n_point_total > n_stimuli:
        # TODO issue warning or handle special case

        # First select all points regardless of set assignment.
        # Initialize greedy search.
        candidate_idx = np.random.permutation(n_stimuli)
        best_val = 0

        def obj_func_1(z_candidate):
            # Select those far from mean and far from one another.
            rho = 2.
            n_stim = z_candidate.shape[0]
            loss1 = np.sum(np.abs(z_candidate)**(rho))**(1/rho) / n_stim
            loss2 = np.mean(pdist(z_candidate))
            return (.5 * loss1) + loss2

        n_step = 100
        for _ in range(n_step):
            # Swap a selected point out with a non-selected point.
            selected_idx = randint(0, n_point_total-1)
            nonselected_idx = randint(n_point_total, n_stimuli-1)
            copied_idx = copy.copy(candidate_idx[selected_idx])
            candidate_idx[selected_idx] = candidate_idx[nonselected_idx]
            candidate_idx[nonselected_idx] = copied_idx

            # Evaluate candidate
            z_candidate = z[candidate_idx[0:n_point_total], :]
            candidate_val = obj_func_1(z_candidate)
            if candidate_val > best_val:
                best_val = copy.copy(candidate_val)
                best_idx = copy.copy(candidate_idx)
        best_idx = best_idx[0:n_point_total]

        # Now that points are selected, assign points such that points
        # within a set are far from each other.
        # Initialize greedy search.
        def obj_func_2(z_candidate, n_point):
            # Create two sets such that members within a set are far from
            # one another.
            loss1 = np.mean(pdist(z_candidate[0:n_point]))
            loss2 = np.mean(pdist(z_candidate[n_point:]))
            return loss1 + loss2

        candidate_idx = best_idx
        best_val = 0

        n_step = 2 * n_point**2
        for _ in range(n_step):
            # Swap a selected point out with a non-selected point.
            selected_idx = randint(0, n_point-1)
            nonselected_idx = randint(n_point, (2 * n_point) - 1)
            copied_idx = copy.copy(candidate_idx[selected_idx])
            candidate_idx[selected_idx] = candidate_idx[nonselected_idx]
            candidate_idx[nonselected_idx] = copied_idx

            # Evaluate candidate
            z_candidate = z[candidate_idx, :]
            candidate_val = obj_func_2(z_candidate, n_point)
            if candidate_val > best_val:
                best_val = copy.copy(candidate_val)
                best_idx = copy.copy(candidate_idx)

        anchor_idx = np.zeros((n_point, n_set), dtype=np.int32)
        anchor_idx[:, 0] = best_idx[0:n_point]
        anchor_idx[:, 1] = best_idx[n_point:]
        return anchor_idx

    def _create_sample_idx(self, n_stimuli, anchor_idx):
        """Create sampling index from anchor index."""
        n_set = 2
        n_anchor_point = anchor_idx.shape[0]
        dmy_idx = np.arange(0, n_stimuli)

        sample_idx = np.empty(
            (n_stimuli - n_anchor_point, n_set), dtype=np.int32)
        good_locs = np.ones((n_stimuli, n_set), dtype=bool)
        for i_set in range(n_set):
            for i_point in range(n_anchor_point):
                good_locs[anchor_idx[i_point, i_set], i_set] = False
            sample_idx[:, i_set] = dmy_idx[good_locs[:, i_set]]
        return sample_idx

    def _inflate_sigma(self, sigma, n_stimuli, n_dim):
        """Exploit covariance matrix trick."""
        sigma_inflated = np.zeros((n_stimuli * n_dim, n_stimuli * n_dim))
        for i_stimuli in range(n_stimuli):
            start_idx = i_stimuli * n_dim
            end_idx = ((i_stimuli + 1) * n_dim)
            sigma_inflated[start_idx:end_idx, start_idx:end_idx] = sigma
        return sigma_inflated

    def save(self, filepath):
        """Save the PsychologialEmbedding model as an HDF5 file.

        Arguments:
            filepath: String specifying the path to save the model.

        """
        f = h5py.File(filepath, "w")
        f.create_dataset("embedding_type", data=type(self).__name__)
        f.create_dataset("n_stimuli", data=self.n_stimuli)
        f.create_dataset("n_dim", data=self.n_dim)
        f.create_dataset("n_group", data=self.n_group)

        grp_z = f.create_group("z")
        grp_z.create_dataset("value", data=self.z["value"])
        grp_z.create_dataset("trainable", data=self.z["trainable"])

        grp_theta = f.create_group("theta")
        for theta_param_name in self.theta:
            grp_theta_param = grp_theta.create_group(theta_param_name)
            grp_theta_param.create_dataset(
                "value", data=self.theta[theta_param_name]["value"])
            grp_theta_param.create_dataset(
                "trainable", data=self.theta[theta_param_name]["trainable"])
            # grp_theta_param.create_dataset(
            #   "bounds", data=self.theta[theta_param_name]["bounds"])

        grp_phi = f.create_group("phi")
        for phi_param_name in self.phi:
            grp_phi_param = grp_phi.create_group(phi_param_name)
            grp_phi_param.create_dataset(
                "value", data=self.phi[phi_param_name]["value"])
            grp_phi_param.create_dataset(
                "trainable", data=self.phi[phi_param_name]["trainable"])

        f.close()


class Exponential(PsychologicalEmbedding):
    """An exponential family stochastic display embedding algorithm.

    This embedding technique uses the following similarity kernel:
        s(x,y) = exp(-beta .* norm(x - y, rho).^tau) + gamma,
    where x and y are n-dimensional vectors. The similarity kernel has
    four free parameters: rho, tau, gamma, and beta. The exponential
    family is obtained by integrating across various psychological
    theories [1,2,3,4].

    References:
        [1] Jones, M., Love, B. C., & Maddox, W. T. (2006). Recency
            effects as a window to generalization: Separating
            decisional and perceptual sequential effects in category
            learning. Journal of Experimental Psychology: Learning,
            Memory, & Cognition, 32 , 316-332.
        [2] Jones, M., Maddox, W. T., & Love, B. C. (2006). The role of
            similarity in generalization. In Proceedings of the 28th
            annual meeting of the cognitive science society (pp. 405-
            410).
        [3] Nosofsky, R. M. (1986). Attention, similarity, and the
            identication-categorization relationship. Journal of
            Experimental Psychology: General, 115, 39-57.
        [4] Shepard, R. N. (1987). Toward a universal law of
            generalization for psychological science. Science, 237,
            1317-1323.

    """

    def __init__(self, n_stimuli, n_dim=2, n_group=1):
        """Initialize.

        Arguments:
            n_stimuli: An integer indicating the total number of unique
                stimuli that will be embedded.
            n_dim (optional): An integer indicating the dimensionalty
                of the embedding.
            n_group (optional): An integer indicating the number of
                different population groups in the embedding. A
                separate set of attention weights will be inferred for
                each group.
        """
        PsychologicalEmbedding.__init__(self, n_stimuli, n_dim, n_group)

        # Default inference settings.
        # self.lr = 0.003
        self.lr = 0.001  # TODO

    def _init_theta(self):
        """Return dictionary of default theta parameters.

        Returns:
            Dictionary of theta parameters.

        """
        theta = dict(
            rho=dict(value=2., trainable=True, bounds=[1., None]),
            tau=dict(value=1., trainable=True, bounds=[1., None]),
            gamma=dict(value=0., trainable=True, bounds=[0., None]),
            beta=dict(value=10., trainable=True, bounds=[1., None])
        )
        return theta

    def _get_similarity_parameters_cold(self):
        """Return a dictionary of TensorFlow parameters.

        Parameters are initialized by sampling from a relatively large
        set.

        Returns:
            tf_theta: A dictionary of algorithm-specific TensorFlow
                variables.

        """
        tf_theta = {}
        if self.theta['rho']['trainable']:
            tf_theta['rho'] = tf.get_variable(
                "rho", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 3.)
            )
        if self.theta['tau']['trainable']:
            tf_theta['tau'] = tf.get_variable(
                "tau", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 2.)
            )
        if self.theta['gamma']['trainable']:
            tf_theta['gamma'] = tf.get_variable(
                "gamma", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(0., .001)
            )
        if self.theta['beta']['trainable']:
            tf_theta['beta'] = tf.get_variable(
                "beta", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 30.)
            )
        return tf_theta

    def _tf_similarity(self, z_q, z_r, tf_theta, tf_attention):
        """Exponential family similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            tf_theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            tf_attention: The weights allocated to each dimension
                in a weighted minkowski metric.
                shape = (n_trial, n_dim)

        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        # Algorithm-specific parameters governing the similarity kernel.
        rho = tf_theta['rho']
        tau = tf_theta['tau']
        gamma = tf_theta['gamma']
        beta = tf_theta['beta']

        # Weighted Minkowski distance.
        d_qref = tf.pow(tf.abs(z_q - z_r), rho)
        d_qref = tf.multiply(d_qref, tf_attention)
        d_qref = tf.pow(tf.reduce_sum(d_qref, axis=1), 1. / rho)

        # Exponential family similarity kernel.
        sim_qr = tf.exp(tf.negative(beta) * tf.pow(d_qref, tau)) + gamma
        return sim_qr

    def _similarity(self, z_q, z_r, theta, attention):
        """Exponential family similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            attention: The weights allocated to each dimension
                in a weighted minkowski metric.
                shape = (n_trial, n_dim)

        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        # Algorithm-specific parameters governing the similarity kernel.
        rho = theta['rho']['value']
        tau = theta['tau']['value']
        gamma = theta['gamma']['value']
        beta = theta['beta']['value']

        # Weighted Minkowski distance.
        d_qref = (np.abs(z_q - z_r))**rho
        d_qref = np.multiply(d_qref, attention)
        d_qref = np.sum(d_qref, axis=1)**(1. / rho)  # TODO faster to sum over first dimension?

        # Exponential family similarity kernel.
        sim_qr = np.exp(np.negative(beta) * d_qref**tau) + gamma
        return sim_qr


class HeavyTailed(PsychologicalEmbedding):
    """A heavy-tailed family stochastic display embedding algorithm.

    This embedding technique uses the following similarity kernel:
        s(x,y) = (kappa + (norm(x-y, rho).^tau)).^(-alpha),
    where x and y are n-dimensional vectors. The similarity kernel has
    four free parameters: rho, tau, kappa, and alpha. The
    heavy-tailed family is a generalization of the Student-t family.
    """

    def __init__(self, n_stimuli, n_dim=2, n_group=1):
        """Initialize.

        Arguments:
            n_stimuli: An integer indicating the total number of unique
                stimuli that will be embedded.
            n_dim (optional): An integer indicating the dimensionalty
                of the embedding.
            n_group (optional): An integer indicating the number of
                different population groups in the embedding. A
                separate set of attention weights will be inferred for
                each group.
        """
        PsychologicalEmbedding.__init__(self, n_stimuli, n_dim, n_group)

        # Default inference settings.
        self.lr = 0.003

    def _init_theta(self):
        """Return dictionary of default theta parameters.

        Returns:
            Dictionary of theta parameters.

        """
        theta = dict(
            rho=dict(value=2., trainable=True, bounds=[1., None]),
            tau=dict(value=1., trainable=True, bounds=[1., None]),
            kappa=dict(value=2., trainable=True, bounds=[0., None]),
            alpha=dict(value=30., trainable=True, bounds=[0., None])
        )
        return theta

    def _get_similarity_parameters_cold(self):
        """Return a dictionary of TensorFlow parameters.

        Parameters are initialized by sampling from a relatively large
        set.

        Returns:
            tf_theta: A dictionary of algorithm-specific TensorFlow
                variables.

        """
        tf_theta = {}
        if self.theta['rho']['trainable']:
            tf_theta['rho'] = tf.get_variable(
                "rho", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 3.)
            )
        if self.theta['tau']['trainable']:
            tf_theta['tau'] = tf.get_variable(
                "tau", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 2.)
            )
        if self.theta['kappa']['trainable']:
            tf_theta['kappa'] = tf.get_variable(
                "kappa", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 11.)
            )
        if self.theta['alpha']['trainable']:
            tf_theta['alpha'] = tf.get_variable(
                "alpha", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(10., 60.)
            )
        return tf_theta

    def _tf_similarity(self, z_q, z_r, tf_theta, tf_attention):
        """Heavy-tailed family similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            tf_theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            tf_attention: The weights allocated to each dimension
                in a weighted minkowski metric.
                shape = (n_trial, n_dim)

        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        # Algorithm-specific parameters governing the similarity kernel.
        rho = tf_theta['rho']
        tau = tf_theta['tau']
        kappa = tf_theta['kappa']
        alpha = tf_theta['alpha']

        # Weighted Minkowski distance.
        d_qref = tf.pow(tf.abs(z_q - z_r), rho)
        d_qref = tf.multiply(d_qref, tf_attention)
        d_qref = tf.pow(tf.reduce_sum(d_qref, axis=1), 1. / rho)

        # Heavy-tailed family similarity kernel.
        sim_qr = tf.pow(kappa + tf.pow(d_qref, tau), (tf.negative(alpha)))
        return sim_qr

    def _similarity(self, z_q, z_r, theta, attention):
        """Heavy-tailed family similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            attention: The weights allocated to each dimension
                in a weighted minkowski metric.
                shape = (n_trial, n_dim)

        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        # Algorithm-specific parameters governing the similarity kernel.
        rho = theta['rho']['value']
        tau = theta['tau']['value']
        kappa = theta['kappa']['value']
        alpha = theta['alpha']['value']

        # Weighted Minkowski distance.
        d_qref = (np.abs(z_q - z_r))**rho
        d_qref = np.multiply(d_qref, attention)
        d_qref = np.sum(d_qref, axis=1)**(1. / rho)

        # Heavy-tailed family similarity kernel.
        sim_qr = (kappa + d_qref**tau)**(np.negative(alpha))
        return sim_qr


class StudentsT(PsychologicalEmbedding):
    """A Student's t family stochastic display embedding algorithm.

    The embedding technique uses the following simialrity kernel:
        s(x,y) = (1 + (((norm(x-y, rho)^tau)/alpha))^(-(alpha + 1)/2),
    where x and y are n-dimensional vectors. The similarity kernel has
    three free parameters: rho, tau, and alpha. The original Student-t
    kernel proposed by van der Maaten [1] uses the parameter settings
    rho=2, tau=2, and alpha=n_dim-1. By default, this embedding
    algorithm will only infer the embedding and not the free parameters
    associated with the similarity kernel. This behavior can be changed
    by setting the inference flags (e.g.,infer_alpha = True).

    References:
    [1] van der Maaten, L., & Weinberger, K. (2012, Sept). Stochastic
        triplet embedding. In Machine learning for signal processing
        (mlsp), 2012 IEEE international workshop on (p. 1-6).
        doi:10.1109/MLSP.2012.6349720

    """

    def __init__(self, n_stimuli, n_dim=2, n_group=1):
        """Initialize.

        Arguments:
            n_stimuli: An integer indicating the total number of unique
                stimuli that will be embedded.
            n_dim (optional): An integer indicating the dimensionalty
                of the embedding.
            n_group (optional): An integer indicating the number of
                different population groups in the embedding. A
                separate set of attention weights will be inferred for
                each group.
        """
        PsychologicalEmbedding.__init__(self, n_stimuli, n_dim, n_group)

        # Default inference settings.
        self.lr = 0.01

    def _init_theta(self):
        """Return dictionary of default theta parameters.

        Returns:
            Dictionary of theta parameters.

        """
        theta = dict(
            rho=dict(value=2., trainable=False, bounds=[1., None]),
            tau=dict(value=2., trainable=False, bounds=[1., None]),
            alpha=dict(
                value=(self.n_dim - 1.),
                trainable=False,
                bounds=[0.000001, None]
            ),
        )
        return theta

    def _get_similarity_parameters_cold(self):
        """Return a dictionary of TensorFlow parameters.

        Parameters are initialized by sampling from a relatively large
        set.

        Returns:
            tf_theta: A dictionary of algorithm-specific TensorFlow
                variables.

        """
        tf_theta = {}
        if self.theta['rho']['trainable']:
            tf_theta['rho'] = tf.get_variable(
                "rho", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 3.)
            )
        if self.theta['tau']['trainable']:
            tf_theta['tau'] = tf.get_variable(
                "tau", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(1., 2.)
            )
        if self.theta['alpha']['trainable']:
            min_alpha = np.max((1, self.n_dim - 5.))
            max_alpha = self.n_dim + 5.
            tf_theta['alpha'] = tf.get_variable(
                "alpha", [1], dtype=FLOAT_X,
                initializer=tf.random_uniform_initializer(
                    min_alpha, max_alpha
                )
            )
        return tf_theta

    def _tf_similarity(self, z_q, z_r, tf_theta, tf_attention):
        """Student-t family similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            tf_theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            tf_attention: The weights allocated to each dimension
                in a weighted minkowski metric.
                shape = (n_trial, n_dim)

        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        # Algorithm-specific parameters governing the similarity kernel.
        rho = tf_theta['rho']
        tau = tf_theta['tau']
        alpha = tf_theta['alpha']

        # Weighted Minkowski distance.
        d_qref = tf.pow(tf.abs(z_q - z_r), rho)
        d_qref = tf.multiply(d_qref, tf_attention)
        d_qref = tf.pow(tf.reduce_sum(d_qref, axis=1), 1. / rho)

        # Student-t family similarity kernel.
        sim_qr = tf.pow(
            1 + (tf.pow(d_qref, tau) / alpha), tf.negative(alpha + 1)/2)
        return sim_qr

    def _similarity(self, z_q, z_r, theta, attention):
        """Student-t family similarity kernel.

        Arguments:
            z_q: A set of embedding points.
                shape = (n_trial, n_dim)
            z_r: A set of embedding points.
                shape = (n_trial, n_dim)
            tf_theta: A dictionary of algorithm-specific parameters
                governing the similarity kernel.
            tf_attention: The weights allocated to each dimension
                in a weighted minkowski metric.
                shape = (n_trial, n_dim)

        Returns:
            The corresponding similarity between rows of embedding
                points.
                shape = (n_trial,)

        """
        # Algorithm-specific parameters governing the similarity kernel.
        rho = theta['rho']['value']
        tau = theta['tau']['value']
        alpha = theta['alpha']['value']

        # Weighted Minkowski distance.
        d_qref = (np.abs(z_q - z_r))**rho
        d_qref = np.multiply(d_qref, attention)
        d_qref = np.sum(d_qref, axis=1)**(1. / rho)

        # Student-t family similarity kernel.
        sim_qr = (1 + (d_qref**tau / alpha))**(np.negative(alpha + 1)/2)
        return sim_qr


def _assert_float_dtype(dtype):
    """Validate and return floating point type based on `dtype`.
    `dtype` must be a floating point type.
    Args:
        dtype: The data type to validate.
    Returns:
        Validated type.
    Raises:
        ValueError: if `dtype` is not a floating point type.

    """
    if not dtype.is_floating:
        raise ValueError("Expected floating point type, got %s." % dtype)
    return dtype


class RandomEmbedding(Initializer):
    """Initializer that generates tensors with a normal distribution.

    Arguments:
        mean: A python scalar or a scalar tensor. Mean of the random
            values to generate.
        minval: TODO
        maxval: TODO
        seed: A Python integer. Used to create random seeds. See
        `tf.set_random_seed` for behavior.
        dtype: The data type. Only floating point types are supported.
    """

    def __init__(self, mean=0.0, stdev=1.0, minval=0.0, maxval=0.0, seed=None, dtype=tf.float32):
        self.mean = mean
        self.stdev = stdev
        self.minval = minval
        self.maxval = maxval
        self.seed = seed
        self.dtype = _assert_float_dtype(dtype)

    def __call__(self, shape, dtype=None, partition_info=None):
        if dtype is None:
            dtype = self.dtype
        scale = tf.pow(
            tf.constant(10., dtype=FLOAT_X),
            random_uniform(
                [1],
                minval=self.minval,
                maxval=self.maxval,
                dtype=dtype,
                seed=self.seed,
                name=None
            )
        )
        stdev = scale * self.stdev
        return random_normal(
            shape, self.mean, stdev, dtype, seed=self.seed)

    def get_config(self):
        return {
            "mean": self.mean,
            "stdev": self.stdev,
            "min": self.minval,
            "max": self.maxval,
            "seed": self.seed,
            "dtype": self.dtype.name
        }


class RandomAttention(Initializer):
    """Initializer that generates tensors for attention weights.

    Arguments:
        alpha: A python scalar or a scalar tensor. Alpha parameter(s)
            governing distribution.
        dtype: The data type. Only floating point types are supported.
    """

    def __init__(self, n_dim, concentration=0.0, dtype=tf.float32):
        self.n_dim = n_dim
        self.concentration = concentration
        self.dtype = _assert_float_dtype(dtype)

    def __call__(self, shape, dtype=None, partition_info=None):
        if dtype is None:
            dtype = self.dtype
        dist = tf.distributions.Dirichlet(self.concentration)
        return self.n_dim * dist.sample([shape[0]])

    def get_config(self):
        return {
            "concentration": self.concentration,
            "dtype": self.dtype.name
        }


def load_embedding(filepath):
    """Load embedding model saved via the save method.

    The loaded data is instantiated as a concrete class of
    SimilarityTrials.

    Arguments:
        filepath: The location of the hdf5 file to load.

    Returns:
        Loaded embedding model.

    Raises:
        ValueError

    """
    f = h5py.File(filepath, 'r')
    # Common attributes.
    embedding_type = f['embedding_type'][()]
    n_stimuli = f['n_stimuli'][()]
    n_dim = f['n_dim'][()]
    n_group = f['n_group'][()]

    if embedding_type == 'Exponential':
        embedding = Exponential(n_stimuli, n_dim=n_dim, n_group=n_group)
    elif embedding_type == 'HeavyTailed':
        embedding = HeavyTailed(n_stimuli, n_dim=n_dim, n_group=n_group)
    elif embedding_type == 'StudentsT':
        embedding = StudentsT(n_stimuli, n_dim=n_dim, n_group=n_group)
    else:
        raise ValueError(
            'No class found matching the provided `embedding_type`.')

    for name in f['z']:
        embedding.z[name] = f['z'][name][()]

    for p_name in f['theta']:
        for name in f['theta'][p_name]:
            embedding.theta[p_name][name] = f['theta'][p_name][name][()]

    for p_name in f['phi']:
        for name in f['phi'][p_name]:
            embedding.phi[p_name][name] = f['phi'][p_name][name][()]

    f.close()
    return embedding
