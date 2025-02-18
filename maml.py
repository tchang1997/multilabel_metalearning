###################
# TODO: -create separate utils and models files
#	   -command line args
#	   		-verbose
#			-num iteatrations
#			-learning rate
#			-dynamic learning rate
#		-pretrained model
#		-implement binary relevance
#
##################

import os
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '1'


import random
import sys
import csv
import pickle
import time
#import matplotlib.pyplot as plt
import tensorflow as tf
import numpy as np
from functools import partial
from tqdm import tqdm

import load_data_tf as load_data
import models
from utils import *
from options import get_args
from tensorboardX import SummaryWriter

import logging
from pathlib import Path


seed = 123
IMG_SIZE = 120


class MAML(tf.keras.Model):
    def __init__(self, dim_input=1, dim_output=1, num_inner_updates=1, inner_update_lr=0.4, num_filters=32, learn_inner_update_lr=False, model='VanillaConvModel', multi = 'powerset', num_classes=3):
        super(MAML, self).__init__()
        self.dim_input = dim_input
        self.num_classes = num_classes
        self.dim_output = dim_output
        self.inner_update_lr = inner_update_lr
        self.loss_func = partial(cross_entropy_loss)
        self.dim_hidden = num_filters
        self.channels = 3
        self.img_size = int(np.sqrt(self.dim_input / self.channels))
        self.multi = multi

        # outputs_ts[i] and losses_ts_post[i] are the output and loss after i+1
        # inner gradient updates
        losses_tr_pre, outputs_tr, losses_ts_post, outputs_ts = [], [], [], []
        accuracies_tr_pre, accuracies_ts = [], []

        # for each loop in the inner training loop
        outputs_ts = [[]] * num_inner_updates
        losses_ts_post = [[]] * num_inner_updates
        accuracies_ts = [[]] * num_inner_updates

        # Define the weights - these should NOT be directly modified by the
        # inner training loop
        tf.random.set_seed(seed)

        if hasattr(models, model):
            model_class = getattr(models, model)
            self.inner_model = model_class(self.channels, self.dim_hidden, self.dim_output, self.img_size, self.multi)
        else:
            raise ValueError("Model name '{}' is not supported!")

        self.learn_inner_update_lr = learn_inner_update_lr
        if self.learn_inner_update_lr:
            self.inner_update_lr_dict = {}
            for key in self.inner_model.model_weights.keys():
                self.inner_update_lr_dict[key] = [
                    tf.Variable(self.inner_update_lr, name='inner_update_lr_%s_%d' %
                        (key, j)) for j in range(num_inner_updates)]

    
    @tf.function
    def call(self, inp, meta_batch_size=25, num_inner_updates=1):

        def task_inner_loop(inp, reuse=True,
                            meta_batch_size=25, num_inner_updates=1):
            start = time.time()
            # the inner and outer loop data
            input_tr, input_ts, label_tr, label_ts = inp

            # weights corresponds to the initial weights in MAML 
            weights = self.inner_model.model_weights.copy()

            # the predicted outputs, loss values, and accuracy for the pre-update model (with the initial weights), evaluated on the inner loop training data
            task_output_tr_pre, task_loss_tr_pre = None, None
            task_precision_tr_pre, task_recall_tr_pre, task_f1_tr_pre = None, None, None

            # lists to keep track of outputs, losses, and accuracies of test data for each inner_update
            # where task_outputs_ts[i], tasnum_classes=7k_losses_ts[i], task_accuracies_ts[i] are the output, loss, and accuracy after i+1 inner gradient updates
            task_outputs_ts, task_losses_ts = [], []
            task_precision_ts, task_recall_ts, task_f1_ts = [], [], []

            task_output_tr_pre = self.inner_model(input_tr, weights)
            task_loss_tr_pre = self.loss_func(task_output_tr_pre, label_tr)
            for i in range(num_inner_updates):
                with tf.GradientTape(persistent=True) as tape: # keep track of high-order derivs on train data only, and use those to update
                    for key in weights: tape.watch(weights[key])
                    output_tr = self.inner_model(input_tr, weights) 
                    loss_tr = self.loss_func(output_tr, label_tr)
                grads = dict(zip(weights.keys(), tape.gradient(loss_tr, list(weights.values())))) # might need to make a TF op 
                if self.learn_inner_update_lr:
                    weights = dict(zip(weights.keys(), [weights[key] - self.inner_update_lr_dict[key] * grads[key] for key in weights])) # might need to make a TF op? probably OK
                else:
                    weights = dict(zip(weights.keys(), [weights[key] - self.inner_update_lr * grads[key] for key in weights])) # might need to make a TF op? probably OK

                # post-update metrics on test
                output_ts = self.inner_model(input_ts, weights)
                task_outputs_ts.append(output_ts) # TODO: Make TF op
                task_losses_ts.append(self.loss_func(output_ts, label_ts)) # TODO: Make TF op

            #############################

            # Compute accuracies from output predictions
            label_dense_tr = tf.cast(tf.argmax(input=label_tr, axis=-1), tf.int32)
            preds_tr = tf.cast(tf.argmax(input=tf.nn.softmax(task_output_tr_pre), axis=-1), tf.int32)
            task_precision_tr_pre = precision(label_dense_tr, preds_tr, self.num_classes, multi=self.multi)
            task_recall_tr_pre = recall(label_dense_tr, preds_tr, self.num_classes, multi=self.multi)
            task_f1_tr_pre = fscore(label_dense_tr, preds_tr, self.num_classes, multi=self.multi)

            label_dense_ts = tf.cast(tf.argmax(input=label_ts, axis=-1), tf.int32)
            for j in range(num_inner_updates):
                preds_ts = tf.cast(tf.argmax(input=tf.nn.softmax(task_outputs_ts[j]), axis=-1), tf.int32)
                task_precision_ts.append(precision(label_dense_ts, preds_ts, self.num_classes, multi=self.multi))
                task_recall_ts.append(recall(label_dense_ts, preds_ts, self.num_classes, multi=self.multi))
                task_f1_ts.append(fscore(label_dense_ts, preds_ts, self.num_classes, multi=self.multi))

            task_output = [task_output_tr_pre, task_outputs_ts, task_loss_tr_pre, task_losses_ts, task_precision_tr_pre, task_precision_ts, task_recall_tr_pre, task_recall_ts, task_f1_tr_pre, task_f1_ts]
            #tf.print("Iteration took {}s".format(time.time() - start))
            return task_output

        input_tr, input_ts, label_tr, label_ts = inp
        # to initialize the batch norm vars, might want to combine this, and
        # not run idx 0 twice.
        unused = task_inner_loop((input_tr[0], input_ts[0], label_tr[0], label_ts[0]), False, meta_batch_size, num_inner_updates)
        out_dtype = [tf.float32, [tf.float32] * num_inner_updates] * 5
        #             tf.float32, [tf.float32] * num_inner_updates]
        #out_dtype.extend([tf.float32, [tf.float32] * num_inner_updates])
        task_inner_loop_partial = partial(task_inner_loop, meta_batch_size=meta_batch_size, num_inner_updates=num_inner_updates)
        result = tf.map_fn(task_inner_loop_partial,
                           elems=(input_tr, input_ts, label_tr, label_ts),
                           fn_output_signature=out_dtype,
                           parallel_iterations=meta_batch_size)
        return result


"""Model training code"""
"""
Usage Instructions:
	5-way, 1-shot omniglot:
	python main.py --meta_train_iterations=15000 --meta_batch_size=25 --k_shot=1 --inner_update_lr=0.4 --num_inner_updates=1 --logdir=logs/omniglot5way/
	20-way, 1-shot omniglot:
	python main.py --meta_train_iterations=15000 --meta_batch_size=16 --k_shot=1 --n_way=20 --inner_update_lr=0.1 --num_inner_updates=5 --logdir=logs/omniglot20way/
	To run evaluation, use the '--meta_train=False' flag and the '--meta_test_set=True' flag to use the meta-test set.
	"""


def outer_train_step(inp, model, optim, meta_batch_size=25, num_inner_updates=1):
    with tf.GradientTape(persistent=False) as outer_tape:
        result = model(inp, meta_batch_size=meta_batch_size,
                       num_inner_updates=num_inner_updates)

        outputs_tr, outputs_ts, loss_tr_pre, losses_ts, precision_tr_pre, precision_ts, recall_tr_pre, recall_ts, f1_tr_pre, f1_ts = result
        total_losses_ts = [tf.reduce_mean(loss_ts) for loss_ts in losses_ts]

    gradients = outer_tape.gradient(
        total_losses_ts[-1], model.trainable_variables)
    optim.apply_gradients(zip(gradients, model.trainable_variables))

    total_loss_tr_pre = tf.reduce_mean(loss_tr_pre)
    #total_accuracy_tr_pre = tf.reduce_mean(accuracies_tr_pre)
    #total_accuracies_ts = [tf.reduce_mean(
    #    accuracy_ts) for accuracy_ts in accuracies_ts]
    total_precision_tr_pre = tf.reduce_mean(precision_tr_pre)
    total_precision_ts = [tf.reduce_mean(task_prec_ts) for task_prec_ts in precision_ts]
    total_recall_tr_pre = tf.reduce_mean(recall_tr_pre)
    total_recall_ts = [tf.reduce_mean(task_rec_ts) for task_rec_ts in recall_ts]
    total_f1_tr_pre = tf.reduce_mean(f1_tr_pre)
    total_f1_ts = [tf.reduce_mean(task_f1_ts) for task_f1_ts in f1_ts]

    return outputs_tr, outputs_ts, total_loss_tr_pre, total_losses_ts, total_precision_tr_pre, total_precision_ts, total_recall_tr_pre, total_recall_ts, total_f1_tr_pre, total_f1_ts 


def outer_eval_step(inp, model, meta_batch_size=25, num_inner_updates=1):
    result = model(inp, meta_batch_size=meta_batch_size,
                   num_inner_updates=num_inner_updates)

    outputs_tr, outputs_ts, loss_tr_pre, losses_ts, precision_tr_pre, precision_ts, recall_tr_pre, recall_ts, f1_tr_pre, f1_ts = result


    total_loss_tr_pre = tf.reduce_mean(loss_tr_pre)
    total_losses_ts = [tf.reduce_mean(loss_ts) for loss_ts in losses_ts]

    total_precision_tr_pre = tf.reduce_mean(precision_tr_pre)
    total_precision_ts = [tf.reduce_mean(task_prec_ts) for task_prec_ts in precision_ts]
    total_recall_tr_pre = tf.reduce_mean(recall_tr_pre)
    total_recall_ts = [tf.reduce_mean(task_rec_ts) for task_rec_ts in recall_ts]
    total_f1_tr_pre = tf.reduce_mean(f1_tr_pre)
    total_f1_ts = [tf.reduce_mean(task_f1_ts) for task_f1_ts in f1_ts]

    return outputs_tr, outputs_ts, total_loss_tr_pre, total_losses_ts, total_precision_tr_pre, total_precision_ts, total_recall_tr_pre, total_recall_ts, total_f1_tr_pre, total_f1_ts


def meta_train_fn(model, sampling_mode, exp_string, meta_dataset, writer, support_size=8, meta_train_iterations=15000, meta_batch_size=16, log=True, logdir='/tmp/data', num_inner_updates=1, meta_lr=0.001, log_frequency=5, test_log_frequency=25, multi='powerset'):

    pre_accuracies, post_accuracies = [], []
    pre_loss, post_loss = [], []
    pre_precision, post_precision = [], []
    pre_recall, post_recall = [], []
    pre_f1, post_f1 = [], []

    optimizer = tf.keras.optimizers.Adam(learning_rate=meta_lr)

    plot_accuracies = []
    for itr in range(meta_train_iterations):
        #############################

        # sample a batch of training data and partition into
        # the support/training set (input_tr, label_tr) and the query/test set (input_ts, label_ts)
        # NOTE: The code assumes that the support and query sets have the same
        # number of examples.

        X, y, y_debug = meta_dataset.sample_batch(batch_size=meta_batch_size, split='train', mode=sampling_mode)
        #X = tf.reshape(X, [meta_batch_size, support_size, -1])
        converter = convert_to_powerset if multi == 'powerset' else convert_to_bin_rel
        inp = support_query_split(X, y, converter, support_dim=1)
        """
        input_tr, input_ts = tf.split(X, 2, axis=1)
        single_labels = (np.packbits(y.astype(int), 2, 'little') - 1).reshape((len(y), -1))
        one_hot = np.eye(num_classes)[single_labels]
        label_tr, label_ts = tf.split(one_hot, 2, axis=1)
        """

        #############################

        #inp = (input_tr, input_ts, label_tr, label_ts)
        start = time.time()
        result = outer_train_step(inp, model, optimizer, meta_batch_size=meta_batch_size, num_inner_updates=num_inner_updates)
        outputs_tr, outputs_ts, total_loss_tr_pre, total_losses_ts, total_precision_tr_pre, total_precision_ts, total_recall_tr_pre, total_recall_ts, total_f1_tr_pre, total_f1_ts = result

        # log metrics here
        #pre_accuracies.append(result[-2])
        #post_accuracies.append(result[-1][-1])
        pre_loss.append(result[2])
        post_loss.append(result[3][-1])
        pre_precision.append(result[4])
        post_precision.append(result[5][-1])
        pre_recall.append(result[6])
        post_recall.append(result[7][-1])
        pre_f1.append(result[8])
        post_f1.append(result[9][-1])

        if (itr != 0) and itr % log_frequency == 0:
            #print_str = 'Iteration %d: pre-inner-loop train loss/accuracy: %.5f/%.5f, post-inner-loop validation loss/accuracy: %.5f/%.5f, time elapsed: %.4fs' % (itr, np.mean(pre_loss), np.mean(pre_accuracies), np.mean(post_loss), np.mean(post_accuracies), time.time() - start)
            print_str = "Iteration {}: pre-inner train loss/prec./rec./F1: {:.5f}/{:.5f}/{:.5f}/{:.5f}, post-inner train loss/prec./rec./F1: {:.5f}/{:.5f}/{:.5f}/{:.5f}, time elapsed: {:.4f}s".format(itr, np.mean(pre_loss), np.mean(pre_precision), np.mean(pre_recall), np.mean(pre_f1), np.mean(post_loss), np.mean(post_precision), np.mean(post_recall), np.mean(post_f1), time.time() - start)
            print(print_str)

            writer.add_scalar('Inner loss', np.mean(post_loss), itr)
            writer.add_scalar('Inner precision', np.mean(post_precision), itr)
            writer.add_scalar('Inner recall', np.mean(post_recall), itr)
            writer.add_scalar('Inner F1', np.mean(post_f1), itr)

            pre_accuracies, post_accuracies = [], []
            pre_loss, post_loss = [], []
            pre_precision, post_precision = [], []
            pre_recall, post_recall = [], []
            pre_f1, post_f1 = [], []

        if (itr != 0) and itr % test_log_frequency == 0:

            """
            sample a batch of validation data and partition it into
            the support/training set (input_tr, label_tr) and the query/test set (input_ts, label_ts)
            NOTE: The code assumes that the support and query sets have the
            same number of examples.
            """

            X, y, y_debug = meta_dataset.sample_batch(batch_size=meta_batch_size, split='val', mode=sampling_mode)
            #X = tf.reshape(X, [meta_batch_size, support_size, -1])

            converter = convert_to_powerset if multi == 'powerset' else convert_to_bin_rel
            inp = support_query_split(X, y, converter, support_dim=1)
            # input_tr, input_ts = tf.split(X, 2, axis=1)
            # single_labels = (np.packbits(y.astype(int), 2,'little') - 1).reshape((len(y), -1))
            # one_hot = np.eye(num_classes)[single_labels]
            # label_tr, label_ts = tf.split(one_hot, 2, axis=1)

            # inp = (input_tr, input_ts, label_tr, label_ts)
            result = outer_eval_step(inp, model, meta_batch_size=meta_batch_size, num_inner_updates=num_inner_updates)
            outputs_tr, outputs_ts, total_loss_tr_pre, total_losses_ts, total_precision_tr_pre, total_precision_ts, total_recall_tr_pre, total_recall_ts, total_f1_tr_pre, total_f1_ts = result

            #print('Meta-validation pre-inner-loop train accuracy: %.5f, meta-validation post-inner-loop test accuracy: %.5f' % (result[-2], result[-1][-1]))
            eval_print_str = "Meta-val. pre-inner loss/prec./rec./F1: {:.5f}/{:.5f}/{:.5f}/{:.5f}, meta-val. post-inner loss/prec./rec./F1: {:.5f}/{:.5f}/{:.5f}/{:.5f}".format(total_loss_tr_pre, total_precision_tr_pre, total_recall_tr_pre, total_f1_tr_pre, total_losses_ts[-1], total_precision_ts[-1], total_recall_ts[-1], total_f1_ts[-1])
            print(eval_print_str)

            writer.add_scalar('Outer loss', float(total_losses_ts[-1]), itr)
            writer.add_scalar('Outer precision', float(total_precision_ts[-1]), itr)
            writer.add_scalar('Outer recall', float(total_recall_ts[-1]), itr)
            writer.add_scalar('Outer F1', float(total_f1_ts[-1]), itr)
            #plot_accuracies.append(result[-1][-1])

    #plt.plot(np.arange(50, meta_train_iterations, 50), plot_accuracies)
    #plt.ylabel('Validation Accuracy')
    #plt.title('Question 1.4')
    #plt.show()

    model_file = logdir + '/' + exp_string + '/model' + str(itr)
    print("Saving to ", model_file)
    model.save_weights(model_file)


# calculated for omniglot
NUM_META_TEST_POINTS = 600


def meta_test_fn(model, meta_dataset, sampling_mode, writer, support_size=8, meta_batch_size=25, num_inner_updates=1, multi='powerset'):
    #num_classes = data_generator.num_classes

    np.random.seed(1)
    random.seed(1)

    meta_test_losses, meta_test_precision, meta_test_recall, meta_test_f1 = [],  [], [],  []

    for itr in tqdm(range(NUM_META_TEST_POINTS)):


        # sample a batch of test data and partition it into
        # the support/training set (input_tr, label_tr) and the query/test set (input_ts, label_ts)
        # NOTE: The code assumes that the support and query sets have the same
        # number of examples.

        X, y, y_debug = meta_dataset.sample_batch(batch_size=meta_batch_size, split='test', mode=sampling_mode)
        #X = tf.reshape(X, [meta_batch_size, support_size, -1])
        converter = convert_to_powerset if multi == 'powerset' else convert_to_bin_rel
        inp = support_query_split(X, y, converter, support_dim=1)
        # input_tr, input_ts = tf.split(X, 2, axis=1)
        # single_labels = (np.packbits(y.astype(int), 2, 'little') - 1).reshape((len(y), -1))
        # one_hot = np.eye(num_classes)[single_labels]
        # label_tr, label_ts = tf.split(one_hot, 2, axis=1)

        #############################
        #inp = (input_tr, input_ts, label_tr, label_ts)
        result = outer_eval_step(inp, model, meta_batch_size=meta_batch_size, num_inner_updates=num_inner_updates)
        outputs_tr, outputs_ts, total_loss_tr_pre, total_losses_ts, total_precision_tr_pre, total_precision_ts, total_recall_tr_pre, total_recall_ts, total_f1_tr_pre, total_f1_ts = result

        #eval_print_str = "Meta-test pre-inner loss/prec./rec./F1: {:.5f}/{:.5f}/{:.5f}/{:.5f}, meta-test post-inner loss/prec./rec./F1: {:.5f}/{:.5f}/{:.5f}/{:.5f}".format(total_loss_tr_pre, total_precision_tr_pre, total_recall_tr_pre, total_f1_tr_pre, total_losses_ts[-1], total_precision_ts[-1], total_recall_ts[-1], total_f1_ts[-1])
        #print(eval_print_str)

        meta_test_losses.append(float(total_losses_ts[-1]))
        meta_test_precision.append(float(total_precision_ts[-1]))
        meta_test_recall.append(float(total_recall_ts[-1]))
        meta_test_f1.append(float(total_f1_ts[-1]))
        writer.add_scalar('Meta-test loss', float(total_losses_ts[-1]), itr)
        writer.add_scalar('Meta-test precision', float(total_precision_ts[-1]), itr)
        writer.add_scalar('Meta-test recall', float(total_recall_ts[-1]), itr)
        writer.add_scalar('Meta-test F1', float(total_f1_ts[-1]), itr)

    #meta_test_accuracies = np.array(meta_test_accuracies)
    #means = np.mean(meta_test_accuracies)
    #stds = np.std(meta_test_accuracies)
    #ci95 = 1.96 * stds / np.sqrt(NUM_META_TEST_POINTS)
    print("Mean meta-test loss:", np.mean(meta_test_losses), "+/-", 1.96 * np.std(meta_test_losses) / np.sqrt(NUM_META_TEST_POINTS))
    print("Mean meta-test precision:", np.mean(meta_test_precision), "+/-", 1.96 * np.std(meta_test_precision) / np.sqrt(NUM_META_TEST_POINTS))
    print("Mean meta-test recall:", np.mean(meta_test_recall), "+/-", 1.96 * np.std(meta_test_recall) / np.sqrt(NUM_META_TEST_POINTS))
    print("Mean meta-test F1:", np.mean(meta_test_f1), "+/-", 1.96 * np.std(meta_test_f1) / np.sqrt(NUM_META_TEST_POINTS))


def run_maml(support_size=8, meta_batch_size=4, meta_lr=0.001, inner_update_lr=0.4, num_filters=32, num_inner_updates=1, learn_inner_update_lr=False, resume=False, resume_itr=0, log=True, sampling_mode='greedy', logdir='./checkpoints', data_root="../cs330-storage/", meta_train=True, meta_train_iterations=15000, meta_train_inner_update_lr=-1, label_subset_size=3, log_frequency=5, test_log_frequency=25, experiment_name=None, model_class="VanillaConvModel", multilabel_scheme = 'powerset'):

    experiment_fullname = generate_experiment_name(experiment_name, ['train' if meta_train else 'test', Path(__file__).stem])

    log_dir = '../tensorboard_logs/' + experiment_fullname
    os.makedirs(log_dir, exist_ok=True)
    writer = SummaryWriter(log_dir=log_dir)
    # call data_generator and get data with k_shot*2 samples per class
    #  TODO: if args.multilabel_scheme == 'powerset'

    filter_files = [os.path.join(data_root, 'patches_with_cloud_and_shadow.csv'), os.path.join(data_root, 'patches_with_seasonal_snow.csv')]  # replace with your path

    data_dir = os.path.join(data_root, "SmallEarthNet")
    meta_dataset = load_data.MetaBigEarthNetTaskDataset(data_dir=data_dir, filter_files=filter_files, support_size=2 * support_size, label_subset_size=label_subset_size, split_save_path="smallearthnet.pkl", split_file="smallearthnet.pkl")

    # set up MAML model
    dim_output = 2**label_subset_size - 1 if multilabel_scheme == 'powerset' else label_subset_size
    dim_input = (IMG_SIZE**2) * 3

    model = MAML(dim_input, dim_output, num_inner_updates=num_inner_updates, inner_update_lr=inner_update_lr, num_filters=num_filters, learn_inner_update_lr=learn_inner_update_lr, model=model_class, multi = multilabel_scheme)

    if meta_train_inner_update_lr == -1:
        meta_train_inner_update_lr = inner_update_lr

    exp_string = 'supsize_' + str(support_size) + '.mbs_' + str(meta_batch_size) + '.inner_numstep_' + str(
        num_inner_updates) + '.inner_updatelr_' + str(meta_train_inner_update_lr) + '.learn_inner_update_lr_' + str(learn_inner_update_lr)

    if meta_train:
        meta_train_fn(model, sampling_mode, exp_string, meta_dataset, writer, support_size, meta_train_iterations, meta_batch_size, log, logdir, num_inner_updates, meta_lr, log_frequency=log_frequency, test_log_frequency=test_log_frequency, multi = multilabel_scheme)
    else:
        meta_batch_size = 1

        model_file = tf.train.latest_checkpoint(logdir + '/' + exp_string)
        print("Restoring model weights from ", model_file)
        model.load_weights(model_file)

        meta_test_fn(model, sampling_mode, meta_dataset, writer, support_size, meta_batch_size, num_inner_updates, multi = multilabel_scheme)


def main(args):
    print(args.multilabel_scheme)
    run_maml(support_size=args.support_size, inner_update_lr=args.inner_update_lr,
            num_inner_updates=args.num_inner_updates, meta_train_iterations=args.iterations,
            learn_inner_update_lr=args.learn_inner_lr, meta_train=not args.test,
            label_subset_size=args.label_subset_size, log_frequency=args.log_frequency,
            test_log_frequency=args.test_log_frequency, data_root=args.data_root,
            experiment_name=args.experiment_name, model_class=args.model_class_name,
            sampling_mode=args.sampling_mode, multilabel_scheme = args.multilabel_scheme)

if __name__ == '__main__':
    args = get_args()
    main(args)
