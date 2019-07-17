# -*- coding: utf-8 -*-
# Copyright 2019 The PsiZ Authors. All Rights Reserved.
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

"""Module for testing models.py.

Todo
    * test init
    * freeze and thaw
    * attention
    * heavy-tailed similarity
    * Student's t similarity
    * test subset method
"""


import numpy as np
import pytest
import tensorflow as tf

from psiz.trials import Docket
from psiz.models import Exponential, HeavyTailed, StudentsT, load_embedding


@pytest.fixture(scope="module")
def model_true():
    """Return a ground truth embedding."""
    n_stimuli = 10
    n_dim = 2

    model = Exponential(n_stimuli)
    mean = np.ones((n_dim))
    cov = np.identity(n_dim)
    z = np.random.multivariate_normal(mean, cov, (n_stimuli))
    freeze_options = {
        'z': z,
        'theta': {
            'rho': 2,
            'tau': 1,
            'beta': 1,
            'gamma': 0
        }
    }
    model.freeze(freeze_options)
    return model


@pytest.fixture(scope="module")
def model_true_det():
    """Return a ground truth embedding."""
    n_stimuli = 10
    model = Exponential(n_stimuli)
    z = np.array(
        [
            [0.12737487, 1.3211997],
            [0.8335809, 1.5255479],
            [0.8801151, 0.6451549],
            [0.55950886, 1.8086979],
            [1.9089336, -0.15246096],
            [2.8184545, 2.6377177],
            [0.00032808, 0.94420123],
            [0.21504205, 0.92544436],
            [2.0352089, 0.84319389],
            [-0.04342473, 1.4128358]
        ], dtype=np.float32
    )
    freeze_options = {
        'z': z,
        'theta': {
            'rho': 2,
            'tau': 1,
            'beta': 1,
            'gamma': 0
        }
    }
    model.freeze(freeze_options)
    return model


@pytest.fixture(scope="module")
def docket_0():
    """Return a docket of unjudged trials."""
    stimulus_set = np.array((
        (0, 1, 2, 7, 3),
        (3, 4, 5, 9, 1),
        (1, 8, 9, 2, -1),
        (7, 3, 2, 7, -1),
        (6, 7, 5, 0, -1),
        (2, 1, 0, 6, -1),
        (3, 0, 2, 6, -1),
    ))
    n_select = np.array((
        2, 2, 2, 2, 1, 1, 1
        ), dtype=np.int32)
    docket = Docket(stimulus_set, n_select=n_select)
    return docket


def test_private_exponential_similarity():
    """Test exponential similarity function."""
    n_stimuli = 10
    n_dim = 3
    model = Exponential(n_stimuli, n_dim=n_dim)

    z_q_in = np.array((
        (.11, -.13, .28),
        (.45, .09, -1.45)
    ))

    z_ref_in = np.array((
        (.203, -.78, .120),
        (-.105, -.34, -.278)
    ))

    rho = tf.constant(1.9, dtype=tf.float32)
    tau = tf.constant(2.1, dtype=tf.float32)
    beta = tf.constant(1.11, dtype=tf.float32)
    gamma = tf.constant(.001, dtype=tf.float32)
    sim_params = {'rho': rho, 'tau': tau, 'gamma': gamma, 'beta': beta}

    attention_weights = tf.constant(1., shape=[2, 3])

    z_q = tf.compat.v1.placeholder(tf.float32, [None, n_dim], name='z_q')
    z_ref = tf.compat.v1.placeholder(tf.float32, [None, n_dim], name='z_ref')

    s = model._tf_similarity(z_q, z_ref, sim_params, attention_weights)

    sess = tf.compat.v1.Session()
    s_actual = sess.run(
        s, feed_dict={z_q: z_q_in, z_ref: z_ref_in}
    )
    sess.close()

    s_desired = np.array([0.60972816, 0.10853130])
    np.testing.assert_allclose(s_actual, s_desired)


def test_private_exponential_similarity_broadcast():
    """Test exponential similarity function with multiple references."""
    n_stimuli = 10
    n_dim = 3
    model = Exponential(n_stimuli, n_dim=n_dim)

    z_q_in = np.array((
        (.11, -.13, .28),
        (.45, .09, -1.45)
    ))
    z_q_in = np.expand_dims(z_q_in, axis=2)

    z_ref_0 = np.array((
        (.203, -.78, .120),
        (-.105, -.34, -.278)
    ))
    z_ref_1 = np.array((
        (.302, -.87, .021),
        (-.501, -.43, -.872)
    ))
    z_ref_in = np.stack((z_ref_0, z_ref_1), axis=2)

    attention_weights = tf.constant(1., shape=[2, 3, 1])

    rho = tf.constant(1.9, dtype=tf.float32)
    tau = tf.constant(2.1, dtype=tf.float32)
    beta = tf.constant(1.11, dtype=tf.float32)
    gamma = tf.constant(.001, dtype=tf.float32)
    sim_params = {'rho': rho, 'tau': tau, 'gamma': gamma, 'beta': beta}

    z_q = tf.compat.v1.placeholder(tf.float32, name='z_q')
    z_ref = tf.compat.v1.placeholder(tf.float32, name='z_ref')

    s = model._tf_similarity(z_q, z_ref, sim_params, attention_weights)

    sess = tf.compat.v1.Session()
    s_actual = sess.run(
        s, feed_dict={z_q: z_q_in, z_ref: z_ref_in}
    )
    sess.close()

    s_desired = np.array((
        [0.60972816, 0.48281544],
        [0.10853130, 0.16589911]
    ))
    # print('s_actual:', s_actual)
    # print('s_desired:', s_desired)
    np.testing.assert_allclose(s_actual, s_desired)


def test_public_exponential_similarity():
    """Test similarity function."""
    # Create Exponential model.
    n_stimuli = 10
    n_dim = 3
    model = Exponential(n_stimuli, n_dim=n_dim)
    freeze_options = {
        'theta': {
            'rho': 1.9, 'tau': 2.1, 'beta': 1.11, 'gamma': .001
        }
    }
    model.freeze(freeze_options)

    z_q = np.array((
        (.11, -.13, .28),
        (.45, .09, -1.45)
    ))
    z_ref = np.array((
        (.203, -.78, .120),
        (-.105, -.34, -.278)
    ))
    # TODO test both cases where attention_weights are and are not provided.
    # attention_weights = np.ones((2, 3))
    # s_actual = model.similarity(z_q, z_ref, attention=attention_weights)
    s_actual = model.similarity(z_q, z_ref)
    s_desired = np.array([0.60972816, 0.10853130])
    np.testing.assert_allclose(s_actual, s_desired)


def test_public_exponential_similarity_broadcast():
    """Test similarity function."""
    # Create Exponential model.
    n_stimuli = 10
    n_dim = 3
    model = Exponential(n_stimuli, n_dim=n_dim)
    freeze_options = {
        'theta': {
            'rho': 1.9, 'tau': 2.1, 'beta': 1.11, 'gamma': .001
        }
    }
    model.freeze(freeze_options)

    z_q = np.array((
        (.11, -.13, .28),
        (.45, .09, -1.45)
    ))
    z_q = np.expand_dims(z_q, axis=2)

    z_ref_0 = np.array((
        (.203, -.78, .120),
        (-.105, -.34, -.278)
    ))
    z_ref_1 = np.array((
        (.302, -.87, .021),
        (-.501, -.43, -.872)
    ))
    z_ref = np.stack((z_ref_0, z_ref_1), axis=2)

    # TODO test both cases where attention_weights are and are not provided.
    # attention_weights = np.ones((2, 3))
    # s_actual = model.similarity(z_q, z_ref, attention=attention_weights)
    s_actual = model.similarity(z_q, z_ref)
    s_desired = np.array((
        [0.60972816, 0.48281544],
        [0.10853130, 0.16589911]
    ))
    np.testing.assert_allclose(s_actual, s_desired)


def test_weight_projections():
    """Test projection of attention weights."""
    # Create Exponential model.
    n_stimuli = 10
    n_dim = 3
    model = Exponential(n_stimuli, n_dim=n_dim)
    attention = np.array(
        (
            (1., 1., 1.),
            (2., 1., 1.)
        ), ndmin=2
    )
    # attention = np.array(((2., 1., 1.)), ndmin=2) # TODO

    # Project attention weights.
    total = np.sum(attention, axis=1, keepdims=True)
    attention_desired = n_dim * attention / total

    tf_attention = tf.convert_to_tensor(
        attention, dtype=tf.float32
    )
    attention_actual_op = model._project_attention(tf_attention)
    sess = tf.compat.v1.Session()
    attention_actual = sess.run(attention_actual_op)
    sess.close()

    np.testing.assert_allclose(attention_actual, attention_desired)


def test_freeze():
    """Test freeze method."""
    n_stimuli = 10
    n_dim = 2
    n_group = 2
    model = Exponential(n_stimuli, n_dim, n_group)

    model.freeze({'z': np.ones((n_stimuli, n_dim))})
    with pytest.raises(Exception):
        model.freeze({'z': np.ones((n_stimuli-1, n_dim))})
    with pytest.raises(Exception):
        model.freeze({'z': np.ones((n_stimuli, n_dim-1))})

    model.freeze({'phi': {'phi_1': np.ones((n_group, n_dim))}})
    with pytest.raises(Exception):
        model.freeze({'phi': {'phi_1': np.ones((n_group+1, n_dim))}})
    with pytest.raises(Exception):
        model.freeze({'phi': {'phi_1': np.ones((n_group, n_dim-1))}})


def test_probability(model_true, docket_0):
    """Test probability method."""
    prob = model_true.outcome_probability(docket_0)
    prob_actual = np.sum(prob, axis=1)
    prob_desired = np.ones((docket_0.n_trial))
    np.testing.assert_allclose(prob_actual, prob_desired)


def test_tf_probability(model_true, docket_0):
    """Test _tf_outcome_probability method."""
    prob_desired = np.ones((docket_0.n_trial))

    prob_1 = model_true.outcome_probability(docket_0)
    prob_actual_1 = np.sum(prob_1, axis=1)

    z_tf = model_true.z['value']
    z_tf = tf.convert_to_tensor(
        z_tf, dtype=tf.float32
    )

    tf_theta = {}
    for param_name in model_true.theta:
        tf_theta[param_name] = tf.constant(
            model_true.theta[param_name]['value'], dtype=tf.float32)
    prob_2_tf = model_true._tf_outcome_probability(
        docket_0, z_tf, tf_theta)

    sess = tf.compat.v1.Session()
    with sess.as_default():
        sess.run(tf.compat.v1.global_variables_initializer())
        prob_2 = prob_2_tf.eval()

    np.testing.assert_allclose(prob_actual_1, prob_desired)
    np.testing.assert_allclose(prob_1, prob_2, rtol=1e-6)


def test_inflate_points_single_sample(
        model_true_det, docket_0):
    """Test inflation with z with 1 sample."""
    n_reference = 4
    n_select = 2
    trial_locs = np.logical_and(
        docket_0.n_reference == n_reference,
        docket_0.n_select == n_select
    )

    z = model_true_det.z['value']
    (z_q, z_r) = model_true_det._inflate_points(
        docket_0.stimulus_set[trial_locs], n_reference,
        np.expand_dims(z, axis=2)
    )

    z_q_desired = np.array(
        [[0.12737487, 1.3211997], [0.55950886, 1.8086979]],
        dtype=np.float32)
    z_q_desired = np.expand_dims(z_q_desired, axis=2)
    z_q_desired = np.expand_dims(z_q_desired, axis=3)
    np.testing.assert_allclose(z_q, z_q_desired, rtol=1e-6)

    z_r_desired = np.array(
        [
            [
                [0.83358091, 0.88011509, 0.21504205, 0.55950886],
                [1.52554786, 0.64515489, 0.92544436, 1.80869794]
            ],
            [
                [1.90893364, 2.8184545, -0.04342473, 0.83358091],
                [-0.15246096, 2.63771772, 1.41283584, 1.52554786]
            ]
        ], dtype=np.float32)
    z_r_desired = np.expand_dims(z_r_desired, axis=3)
    np.testing.assert_allclose(z_r, z_r_desired, rtol=1e-6)


def test_inflate_points_multiple_samples(model_true_det):
    """Test inflation when z contains samples."""
    n_stimuli = 7
    n_dim = 2
    n_sample = 10
    n_reference = 3
    n_trial = 5
    stimulus_set = np.array((
        (0, 1, 2, 3),
        (3, 4, 0, 1),
        (3, 5, 6, 1),
        (1, 4, 5, 6),
        (2, 1, 3, 6),
    ))
    mean = np.zeros((n_dim))
    cov = .1 * np.identity(n_dim)
    z = np.random.multivariate_normal(mean, cov, (n_sample, n_stimuli))
    z = np.transpose(z, axes=[1, 2, 0])

    # Desired inflation outcome.
    z_q_desired = z[stimulus_set[:, 0], :, :]
    z_q_desired = np.expand_dims(z_q_desired, axis=2)
    z_r_desired = np.empty((n_trial, n_dim, n_reference, n_sample))
    for i_ref in range(n_reference):
        z_r_desired[:, :, i_ref, :] = z[stimulus_set[:, 1+i_ref], :]

    (z_q, z_r) = model_true_det._inflate_points(
        stimulus_set, n_reference, z)

    np.testing.assert_allclose(z_q, z_q_desired, rtol=1e-6)
    np.testing.assert_allclose(z_r, z_r_desired, rtol=1e-6)


def test_tf_ranked_sequence_probability(model_true, docket_0):
    """Test tf_ranked_sequence_probability."""
    docket = docket_0
    n_reference = 4
    n_select = 2
    trial_locs = np.logical_and(
        docket.n_reference == n_reference,
        docket.n_select == n_select
    )

    z = model_true.z['value']

    attention = model_true.phi['phi_1']['value'][0, :]
    attention = np.matlib.repmat(attention, docket_0.n_trial, 1)

    (z_q, z_r) = model_true._inflate_points(
        docket.stimulus_set[trial_locs], n_reference,
        np.expand_dims(z, axis=2)
    )
    s_qref = model_true.similarity(z_q, z_r, group_id=0)
    prob_1 = model_true._ranked_sequence_probabiltiy(s_qref, n_select)
    prob_1 = prob_1[:, 0]

    # NOTE: tf_ranked_sequence_probability is not implemented to handle
    # samples.
    tf_n_select = tf.constant(n_select, dtype=tf.int32)
    tf_s_qref = tf.convert_to_tensor(s_qref[:, :, 0], dtype=tf.float32)
    tf_prob_2 = model_true._tf_ranked_sequence_probability(
        tf_s_qref, tf_n_select)
    sess = tf.compat.v1.Session()
    with sess.as_default():
        sess.run(tf.compat.v1.global_variables_initializer())
        prob_2 = tf_prob_2.eval()

    np.testing.assert_allclose(prob_1, prob_2, rtol=1e-6)


def test_tuning_distance():
    """Test alternative formulation of distance."""
    z_q = np.array((
        (.11, -.13, .28),
        (.45, .09, -1.45),
        (.21, .14, .58),
        (-.91, -.41, -.19)
    ))

    z_r = np.array((
        (.20, -.78, .12),
        (-.10, -.34, -.28),
        (.03, .38, -.12),
        (-.15, -.42, -.78)
    ))

    rho = 2

    attention = np.array((
        (1., 1.2, .8),
        (1., 1.2, .8),
        (1., 1.2, .8),
        (1., 1.2, .8)
    ))

    d_qr_0 = (np.abs(z_q - z_r))**rho
    d_qr_0 = np.multiply(d_qr_0, attention)
    d_qr_0 = np.sum(d_qr_0, axis=1)**(1. / rho)

    # Note: The weight matrix is 3D tensor, first dimension corresponds to the
    # pair being compared.
    # Note: matmul treats the last two dimensions as the actual matrices and
    # broadcasts appropriately.

    # Common weight matrix.
    w1 = np.array((
        (1., 0., 0.),
        (0., 1.2, 0.),
        (0., 0., .8)
    ))
    w = np.expand_dims(w1, axis=0)
    x = np.expand_dims(np.abs(z_q - z_r)**(rho / 2), axis=2)
    x_t = np.transpose(x, axes=(0, 2, 1))
    d_qr_1 = np.matmul(x_t, w)
    d_qr_1 = np.matmul(d_qr_1, x)
    d_qr_1 = d_qr_1**(1 / rho)
    d_qr_1 = np.squeeze(d_qr_1)
    np.testing.assert_array_almost_equal(d_qr_0, d_qr_1)

    # Separate weight matrix.
    attention = np.array((
        (1., 1.2, .8),
        (1., 1.2, .8),
        (.7, 1., 1.3),
        (.7, 1., 1.3),
    ))
    d_qr_0 = (np.abs(z_q - z_r))**rho
    d_qr_0 = np.multiply(d_qr_0, attention)
    d_qr_0 = np.sum(d_qr_0, axis=1)**(1. / rho)

    w1 = np.array((
        (1., 0., 0.),
        (0., 1.2, 0.),
        (0., 0., .8)
    ))
    w1 = np.tile(w1, [2, 1, 1])
    w2 = np.array((
        (.7, 0., 0.),
        (0., 1., 0.),
        (0., 0., 1.3)
    ))
    w2 = np.tile(w2, [2, 1, 1])
    w = np.concatenate((w1, w2), axis=0)
    x = np.expand_dims(np.abs(z_q - z_r)**(rho / 2), axis=2)
    x_t = np.transpose(x, axes=(0, 2, 1))
    d_qr_1 = np.matmul(x_t, w)
    d_qr_1 = np.matmul(d_qr_1, x)
    d_qr_1 = d_qr_1**(1 / rho)
    d_qr_1 = np.squeeze(d_qr_1)
    np.testing.assert_array_almost_equal(d_qr_0, d_qr_1)


def test_tuning_distance_with_multiple_references():
    """Test alternative formulation of distance."""
    z_q = np.array((
        (.11, -.13, .28),
        (.45, .09, -1.45),
        (.21, .14, .58),
        (-.91, -.41, -.19)
    ))
    z_q = np.expand_dims(z_q, axis=2)

    z_r = np.array((
        (.20, -.78, .12),
        (-.10, -.34, -.28),
        (.03, .38, -.12),
        (-.15, -.42, -.78)
    ))
    z_r = np.expand_dims(z_r, axis=2)
    z_r = np.tile(z_r, [1, 1, 2])

    rho = 2

    attention = np.array((
        (1., 1.2, .8),
        (1., 1.2, .8),
        (1., 1.2, .8),
        (1., 1.2, .8)
    ))
    attention = np.expand_dims(attention, axis=2)

    d_qr_0 = (np.abs(z_q - z_r))**rho
    d_qr_0 = np.multiply(d_qr_0, attention)
    d_qr_0 = np.sum(d_qr_0, axis=1)**(1. / rho)

    # Note: The weight matrix is a 3D tensor, first dimension corresponds to
    # the pair being compared.
    # Note: matmul treats the last two dimensions as the actual matrices and
    # broadcasts appropriately.

    # Common weight matrix.
    w1 = np.array((
        (1., 0., 0.),
        (0., 1.2, 0.),
        (0., 0., .8)
    ))
    w = np.expand_dims(w1, axis=0)
    w = np.expand_dims(w, axis=0)
    x = np.abs(z_q - z_r)**(rho / 2)
    x = np.transpose(x, axes=(0, 2, 1))
    x = np.expand_dims(x, axis=3)

    x_t = np.transpose(x, axes=(0, 1, 3, 2))
    d_qr_1 = np.matmul(x_t, w)
    d_qr_1 = np.matmul(d_qr_1, x)
    d_qr_1 = d_qr_1**(1 / rho)
    d_qr_1 = np.squeeze(d_qr_1)
    np.testing.assert_array_almost_equal(d_qr_0, d_qr_1)

    # Separate weight matrix.
    # attention = np.array((
    #     (1., 1.2, .8),
    #     (1., 1.2, .8),
    #     (.7, 1., 1.3),
    #     (.7, 1., 1.3),
    # ))
    # d_qr_0 = (np.abs(z_q - z_r))**rho
    # d_qr_0 = np.multiply(d_qr_0, attention)
    # d_qr_0 = np.sum(d_qr_0, axis=1)**(1. / rho)

    # w1 = np.array((
    #     (1., 0., 0.),
    #     (0., 1.2, 0.),
    #     (0., 0., .8)
    # ))
    # w1 = np.tile(w1, [2, 1, 1])
    # w2 = np.array((
    #     (.7, 0., 0.),
    #     (0., 1., 0.),
    #     (0., 0., 1.3)
    # ))
    # w2 = np.tile(w2, [2, 1, 1])
    # w = np.concatenate((w1, w2), axis=0)
    # x = np.expand_dims(np.abs(z_q - z_r)**(rho / 2), axis=2)
    # x_t = np.transpose(x, axes=(0, 2, 1))
    # d_qr_1 = np.matmul(x_t, w)
    # d_qr_1 = np.matmul(d_qr_1, x)
    # d_qr_1 = d_qr_1**(1 / rho)
    # d_qr_1 = np.squeeze(d_qr_1)
    # np.testing.assert_array_almost_equal(d_qr_0, d_qr_1)


def test_save_load(model_true_det, tmpdir):
    """Test loading and saving of embedding model."""
    # Save embedding model.
    fn = tmpdir.join('embedding_test.hdf5')
    model_true_det.save(fn)
    # Load the saved embedding model.
    loaded_embedding = load_embedding(fn)
    # Test for equality.
    assert loaded_embedding.n_stimuli == model_true_det.n_stimuli
    assert loaded_embedding.n_dim == model_true_det.n_dim
    assert loaded_embedding.n_group == model_true_det.n_group

    np.testing.assert_array_equal(
        loaded_embedding.z['value'],
        model_true_det.z['value']
    )
    assert loaded_embedding.z['trainable'] == model_true_det.z['trainable']

    assert loaded_embedding.theta == model_true_det.theta

    for param_name in model_true_det.phi:
        np.testing.assert_array_equal(
            loaded_embedding.phi[param_name]['value'],
            model_true_det.phi[param_name]['value']
        )
        assert (
            loaded_embedding.phi[param_name]['trainable'] ==
            model_true_det.phi[param_name]['trainable']
        )


# TODO anchor point test
# color_idx = np.zeros((n_stimuli), dtype=np.int64)
# color_idx[anchor_idx[:, 0]] = 1
# color_idx[anchor_idx[:, 1]] = 2

# import matplotlib
# import matplotlib.pyplot as plt
# cmap = matplotlib.cm.get_cmap('jet')
# norm = matplotlib.colors.Normalize(vmin=0., vmax=2.)
# color_array = cmap(norm(range(3)))
# fig, ax = plt.subplots()

# plt.subplot(1, 1, 1)
# for i_stimulus in range(n_stimuli):
#     plt.scatter(
#         z[i_stimulus, 0], z[i_stimulus, 1],
#         c=color_array[color_idx[i_stimulus], :])
# plt.axis('equal')
# plt.show()
