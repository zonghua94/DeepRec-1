"""
 Copyright (c) 2021, NVIDIA CORPORATION.
 
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

     http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import argparse
import sys, os
sys.path.append(os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "../../../")))
import sparse_operation_kit as sok
import tensorflow as tf
import utils
from dense_models import SOKDemo, TFDemo
import strategy_wrapper
import numpy as np

def check_saved_embedding_variables(args, embedding_variable_names, use_hashtable=True, gpu_num=None):
    filepath = r"./embedding_variables"
    for i, embedding_variable_name in enumerate(embedding_variable_names):
        sok_keys_filename = os.path.join(filepath, embedding_variable_name + r"_keys.file")
        sok_keys = utils.read_binary_file(sok_keys_filename, element_type="long long")
        sok_values_filename = os.path.join(filepath, embedding_variable_name + r"_values.file")
        sok_values = utils.read_binary_file(sok_values_filename, element_type="float")

        sorted_sok_keys, sorted_sok_values = utils.sort_embedding_variables_by_key(sok_keys, sok_values, 
                                                        embedding_vec_size=args.embedding_vec_size[i],
                                                        use_hashtable=use_hashtable, gpu_num=gpu_num)

        tf_values_filename = os.path.join(filepath, r"tf_variable_" + str(i) + r".file")
        tf_values = utils.restore_from_file(tf_values_filename)
        valid_tf_values = utils.get_valid_tf_values(sorted_sok_keys, tf_values[0])

        atol, rtol = 1e-4, 1e-4
        if args.distributed_tool == "horovod":
            atol, rtol = atol * 100, rtol * 100
        vec_size = args.embedding_vec_size[i]
        newshape = tuple([sorted_sok_keys.size, vec_size])
        sorted_sok_values = np.reshape(sorted_sok_values, newshape=newshape)
        allclose = np.allclose(sorted_sok_values, valid_tf_values, atol=atol, rtol=rtol)
        if not allclose:
            raise ValueError(f"\n{sorted_sok_values} \nis not near to \n{valid_tf_values} "
                             f"\nat rotl={rtol}, atol={atol}")
    print("[INFO]: the saved parameters are consistent between sparse operation kit and TensorFlow")


def get_sok_results(args, init_tensors, *random_samples):
    if args.distributed_tool == "onedevice":
        import horovod.tensorflow as hvd
        hvd.init()
        strategy = strategy_wrapper.OneDeviceStrategy()
    elif args.distributed_tool == "horovod":
        import horovod.tensorflow as hvd
        hvd.init()
        strategy = strategy_wrapper.HorovodStrategy()
    else:
        raise ValueError(f"{args.distributed_tool} is not supported.")
    
    with strategy.scope():
        sok_init_op = sok.Init(global_batch_size=args.global_batch_size)

        sok_dense_demo = SOKDemo(max_vocabulary_size_per_gpu=args.max_vocabulary_size_per_gpu,
                                 embedding_vec_size=args.embedding_vec_size,
                                 slot_num=args.slot_num,
                                 nnz_per_slot=args.nnz_per_slot,
                                 use_hashtable=args.use_hashtable,
                                 dynamic_input=args.dynamic_input,
                                 num_of_dense_layers=0)

        emb_opt = utils.get_embedding_optimizer(args.optimizer)(learning_rate=0.1)
        dense_opt = utils.get_dense_optimizer(args.optimizer)(learning_rate=0.1)

    sok_saver = sok.Saver()
    restore_op = list()
    for i, embedding_layer in enumerate(sok_dense_demo.embedding_layers):
        control_inputs = [restore_op[-1]] if restore_op else None
        with tf.control_dependencies(control_inputs):
            if args.restore_params:
                filepath = r"./embedding_variables"
                op = sok_saver.restore_from_file(embedding_layer.embedding_variable, filepath)
            else:
                op = sok_saver.load_embedding_values(embedding_layer.embedding_variable, init_tensors[i])
            restore_op.append(op)

    loss_fn = tf.keras.losses.BinaryCrossentropy(from_logits=True, reduction='none')
    def _replica_loss(labels, logits):
        loss = loss_fn(labels, logits)
        return tf.nn.compute_average_loss(loss, global_batch_size=args.global_batch_size)

    def _train_step(inputs, labels, training):
        def _step_fn(inputs, labels):
            logit, embedding_vector = sok_dense_demo(inputs, training=training)
            loss = _replica_loss(labels, logit)
            emb_var, other_var = sok.split_embedding_variable_from_others(sok_dense_demo.trainable_variables)
            grads = tf.gradients(loss, emb_var + other_var, colocate_gradients_with_ops=True,
                                    unconnected_gradients=tf.UnconnectedGradients.NONE)
            emb_grads, other_grads = grads[:len(emb_var)], grads[len(emb_var):]
            if "plugin" in args.optimizer:
                emb_train_op = emb_opt.apply_gradients(zip(emb_grads, emb_var))
            else:
                with sok.OptimizerScope(emb_var):
                    emb_train_op = emb_opt.apply_gradients(zip(emb_grads, emb_var))
            with tf.control_dependencies([*emb_grads]): 
                # in case NCCL runs concurrently via SOK and horovod
                other_grads = strategy.reduce("sum", other_grads)
            other_train_op = dense_opt.apply_gradients(zip(other_grads, other_var))

            with tf.control_dependencies([emb_train_op, other_train_op]):
                total_loss = strategy.reduce("sum", loss)
                total_loss = tf.identity(total_loss)
                return total_loss, embedding_vector
        return strategy.run(_step_fn, inputs, labels)

    replica_batch_size = args.global_batch_size // args.gpu_num
    dataset = utils.tf_dataset(*random_samples, batchsize=replica_batch_size,
                               to_sparse_tensor=False, repeat=1)
    train_iterator = dataset.make_initializable_iterator()
    iterator_init = train_iterator.initializer

    inputs, labels = train_iterator.get_next()
    graph_results = _train_step(inputs, labels, training=True)
    
    init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())
    if "plugin" in args.optimizer:
        init_op = tf.group(init_op, emb_opt.initializer)

    save_op = list()
    for i, embedding_layer in enumerate(sok_dense_demo.embedding_layers):
        control_inputs = [save_op[-1]] if save_op else None
        with tf.control_dependencies(control_inputs):
            if args.save_params:
                filepath = r"./embedding_variables/"
                utils.try_make_dirs(filepath)
                op = sok_saver.dump_to_file(embedding_layer.embedding_variable, filepath)
            else:
                op = tf.constant(1.0)
        save_op.append(op)

    sok_results = list()

    config = tf.ConfigProto()
    config.log_device_placement = False
    with tf.Session(config=config) as sess:
        sess.run(sok_init_op)
        sess.run([init_op, iterator_init])
        sess.run(restore_op)
        sess.graph.finalize()
        
        for step in range(args.iter_num):
            loss_v, emb_vector_v = sess.run([*graph_results])
            print("*" * 80)
            print(f"Step: {step}, loss: {loss_v}, embedding_vector:\n{emb_vector_v}")
            sok_results.append(emb_vector_v)

        sess.run(save_op)
            
    name = list()
    for embedding_layer in sok_dense_demo.embedding_layers:
        name.append(embedding_layer.embedding_variable.m_var_name)

    return sok_results, name


def get_tf_results(args, init_tensors, *random_samples):
    graph = tf.Graph()
    with graph.as_default():
        tf_dense_demo = TFDemo(vocabulary_size=args.max_vocabulary_size_per_gpu * args.gpu_num,
                            slot_num=args.slot_num,
                            nnz_per_slot=args.nnz_per_slot,
                            embedding_vec_size=args.embedding_vec_size,
                            num_of_dense_layers=0,
                            use_hashtable=False,
                            dynamic_input=False)

        optimizer = utils.get_dense_optimizer(args.optimizer)(learning_rate=0.1)

        loss_fn = tf.keras.losses.BinaryCrossentropy(from_logits=True)
        def _train_step(inputs, labels, training):
            logit, embedding_vector = tf_dense_demo(inputs, training=training)
            loss = loss_fn(labels, logit)
            grads = tf.gradients(loss, tf_dense_demo.trainable_variables, colocate_gradients_with_ops=True, 
                                unconnected_gradients=tf.UnconnectedGradients.NONE)
            train_op = optimizer.apply_gradients(zip(grads, tf_dense_demo.trainable_variables))
            with tf.control_dependencies([train_op]):
                loss = tf.identity(loss)
                return loss, embedding_vector

        dataset = utils.tf_dataset(*random_samples, batchsize=args.global_batch_size,
                                to_sparse_tensor=False, repeat=1)
        train_iterator = dataset.make_initializable_iterator()
        iterator_init = train_iterator.initializer

        inputs, labels = train_iterator.get_next()
        graph_results = _train_step(inputs, labels, training=True)

        init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())

        restore_op = list()
        for i, embedding_layer in enumerate(tf_dense_demo.embedding_layers):
            restore_op.append(embedding_layer.embeddings.assign(tf.concat(init_tensors[i], axis=0)))

        emb_values = list()
        for embedding_layer in tf_dense_demo.embedding_layers:
            if args.save_params:
                filepath = r"./embedding_variables/"
                utils.try_make_dirs(filepath)
                emb_values.append(embedding_layer.embeddings.read_value())
            else:
                emb_values = tf.constant(1.0)

    tf_results = list()
    with tf.Session(graph=graph) as sess:
        sess.run([init_op, iterator_init])
        sess.run(restore_op)
        sess.graph.finalize()

        for step in range(args.iter_num):
            loss_v, embedding_vector_v = sess.run([*graph_results])
            print("*" * 80)
            print(f"step: {step}, loss: {loss_v}, embedding_vector:\n{embedding_vector_v}")
            tf_results.append(embedding_vector_v)

        emb_values_v = sess.run(emb_values)
        if args.save_params:
            for i, value in enumerate(emb_values_v):
                utils.save_to_file(os.path.join(filepath, r"tf_variable_" + str(i) + r".file"),
                                    value)
    name = list()
    for embedding_layer in tf_dense_demo.embedding_layers:
        name.append(embedding_layer.embeddings.name)

    return tf_results, name


def compare_dense_emb_sok_with_tf(args):
    if args.global_batch_size % args.gpu_num != 0:
        raise ValueError(f"global_batch_size: {args.global_batch_size} is not divisible by"
                         f" gpu_num: {args.gpu_num}")

    if args.use_hashtable:
        vocabulary_size = args.max_vocabulary_size_per_gpu * args.gpu_num
    else:
        vocabulary_size = args.max_vocabulary_size_per_gpu

    if args.generate_new_datas:
        replica_batch_size = args.global_batch_size // args.gpu_num
        random_samples = utils.generate_random_samples(num_of_samples=replica_batch_size * args.iter_num,
                                                       vocabulary_size=vocabulary_size,
                                                       slot_num=sum(args.slot_num),
                                                       max_nnz=args.nnz_per_slot,
                                                       use_sparse_mask=False)
        utils.save_to_file(r"./random_samples_" + str(args.rank_idx) + r".file", *random_samples)
    else:
        random_samples = utils.restore_from_file(r"./random_samples_" + str(args.rank_idx) + r".file")

    if args.restore_params:
        filepath = r"./embedding_variables"
        # because we already checked the Variable consistency when saving
        # so that we can directly use TensorFlow Variable file to initialize
        # TF's Variable
        init_tensors = list()
        for i in range(len(args.slot_num)):
            tf_values_filename = os.path.join(filepath, r"tf_variable_" + str(i) + r".file")
            init_tensors.append(utils.restore_from_file(tf_values_filename))
    else:
        init_tensors = list()
        for i in range(len(args.slot_num)):
            init_tensors.append(utils.get_ones_tensor(max_vocab_size_per_gpu=args.max_vocabulary_size_per_gpu,
                                                embedding_vec_size=args.embedding_vec_size[i],
                                                num=args.gpu_num))

    sok_results, embedding_variable_name = get_sok_results(args, init_tensors, *random_samples)
    utils.save_to_file(r"./sok_embedding_vectors_" + str(args.rank_idx) + r".file", *sok_results)

    if args.rank_idx != 0:
        return

    # aggregate dataset from different worker
    dataset_filenames = [r"./random_samples_" + str(rank_idx) + r".file"
                         for rank_idx in range(args.rank_size)]
    random_samples_total = [list() for _ in range(args.iter_num)]
    random_labels_total = [list() for _ in range(args.iter_num)]
    local_batch_size = args.global_batch_size // args.gpu_num
    for rank_idx in range(args.rank_size):
        samples, labels = utils.restore_from_file(dataset_filenames[rank_idx])
        for i in range(args.iter_num):
            random_samples_total[i].extend(samples[i * local_batch_size : (i + 1) * local_batch_size])
            random_labels_total[i].extend(labels[i * local_batch_size : (i + 1) * local_batch_size])
    random_samples_total = np.concatenate(random_samples_total, axis=0)
    random_labels_total = np.concatenate(random_labels_total, axis=0)

    tf_results, _ = get_tf_results(args, init_tensors, random_samples_total, random_labels_total)

    # aggregate sok forward results from different worker
    sok_results_filenames = [r"./sok_embedding_vectors_" + str(rank_idx) + r".file"
                             for rank_idx in range(args.rank_size)]
    sok_results_total = list()
    for filename in sok_results_filenames:
        sok_results = utils.restore_from_file(filename)
        sok_results_total.append(sok_results)

    if len(sok_results_total[0]) != len(tf_results):
        raise ValueError("The length of sok results is not equal to that of tensorflow.")
    if len(sok_results) != args.iter_num:
        raise ValueError("The length of embedding vectors: %d is not equal to iteration number: %d."
                        %(len(sok_results), args.iter_num))

    rtol = 1e-4
    atol = 1e-4
    if args.restore_params:
        rtol, atol = 1e-3, 1e-3
    if args.distributed_tool == "horovod":
        rtol, atol = rtol * 10, atol * 10
    for i in range(args.iter_num):
        sok_vector = np.concatenate([sok_results_total[rank_idx][i]
                                     for rank_idx in range(args.rank_size)], axis=0)
        allclose = np.allclose(sok_vector, tf_results[i], rtol=rtol, atol=atol)
        if not allclose:
            raise ValueError(f"\n{sok_vector} \nis not near to \n{tf_results[i]} \nat rtol={rtol}, atol={atol}")

        print("--------------- step: {}---------------------".format(i))
        print("sok_embedding_vector:\n{}".format(sok_vector))
        print("tf_embedding_vector:\n{}".format(tf_results[i]))

    print(f"\n[INFO]: For {len(args.slot_num)} Dense Embedding layer, using {args.gpu_num} GPUs + {args.optimizer} optimizer, "
          f"using hashtable? {args.use_hashtable}, dynamic_input? {args.dynamic_input}, "
          "the embedding vectors"
          f" obtained from sok and tf are consistent for {args.iter_num} iterations.")

    if args.save_params:
        check_saved_embedding_variables(args, embedding_variable_name, 
                                        use_hashtable=args.use_hashtable, gpu_num=args.gpu_num)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--gpu_num", type=int, help="the number of GPUs used in synchronized training.",
                        required=False, default=1)
    parser.add_argument("--distributed_tool", type=str, help="what is used to do the distributed synchronized training",
                        required=False, choices=["horovod", "onedevice"],
                        default="onedevice")
    parser.add_argument("--iter_num", type=int, help="the number of testing iterations.",
                        required=False, default=50)
    parser.add_argument("--max_vocabulary_size_per_gpu", type=int,
                        required=False, default=1024)
    parser.add_argument("--slot_num", type=int, nargs="+",
                        help="the number of feature fields",
                        required=False, default=1)
    parser.add_argument("--nnz_per_slot", type=int,
                        help="the number of keys in each slot.",
                        required=False, default=1)
    parser.add_argument("--embedding_vec_size", type=int, nargs="+",
                        required=False, default=1)
    parser.add_argument("--global_batch_size", type=int, required=False, default=16)
    parser.add_argument("--optimizer", type=str, required=False, default="adam", 
                        choices=["plugin_adam", "adam", "sgd", "compat_adam"])
    parser.add_argument("--generate_new_datas", type=int, choices=[0, 1],
                        required=False, default=1)
    parser.add_argument("--save_params", type=int, choices=[0, 1],
                        required=False, default=1)
    parser.add_argument("--restore_params", type=int, choices=[0, 1],
                        required=False, default=0)
    parser.add_argument("--use_hashtable", type=int, choices=[0, 1],
                        required=False, default=1)
    parser.add_argument("--dynamic_input", type=int, choices=[0, 1],
                        required=False, default=0)

    args = parser.parse_args()

    args.generate_new_datas = True if args.generate_new_datas == 1 else False
    args.save_params = True if args.save_params == 1 else False
    args.restore_params = True if args.restore_params == 1 else False
    args.use_hashtable = True if args.use_hashtable == 1 else False
    args.dynamic_input = True if args.dynamic_input == 1 else False

    if (args.distributed_tool == "onedevice" and args.gpu_num != 1):
        raise ValueError(f"When 'onedevice' is used as the distributed_tool, "
                         f"gpu_num must be 1, which is {args.gpu_num}")

    if args.distributed_tool == "onedevice":
        available_gpus = ",".join(map(str, range(args.gpu_num)))
        rank_size = args.gpu_num
        rank_idx = 0
    else:
        # gpu_num will be ignored.
        rank_size = os.getenv("OMPI_COMM_WORLD_SIZE")
        if rank_size is None:
            raise ValueError(f"When distributed_tool is set to {args.distributed_tool}, "
                             "mpiexec / mpirun must be used to launch this program.")
        rank_size = int(rank_size)
        rank_idx = int(os.getenv("OMPI_COMM_WORLD_RANK"))

        available_gpus = str(rank_idx)

    os.environ["CUDA_VISIBLE_DEVICES"] = available_gpus

    args.rank_size = rank_size
    args.rank_idx = rank_idx
    args.gpu_num = rank_size

    compare_dense_emb_sok_with_tf(args)
        
    

    