import time
import argparse
import tensorflow as tf
import os
import sys
import math
import collections
from tensorflow.python.client import timeline
import json

from tensorflow.python.ops import partitioned_variables

# Set to INFO for tracking training, default is WARN. ERROR for least messages
tf.logging.set_verbosity(tf.logging.INFO)
print("Using TensorFlow version %s" % (tf.__version__))

CONTINUOUS_COLUMNS = ["I" + str(i) for i in range(1, 14)]  # 1-13 inclusive
CATEGORICAL_COLUMNS = ["C" + str(i) for i in range(1, 27)]  # 1-26 inclusive
LABEL_COLUMN = ["clicked"]
TRAIN_DATA_COLUMNS = LABEL_COLUMN + CONTINUOUS_COLUMNS + CATEGORICAL_COLUMNS
FEATURE_COLUMNS = CONTINUOUS_COLUMNS + CATEGORICAL_COLUMNS
HASH_BUCKET_SIZES = {
    'C1': 2500,
    'C2': 2000,
    'C3': 5000000,
    'C4': 1500000,
    'C5': 1000,
    'C6': 100,
    'C7': 20000,
    'C8': 4000,
    'C9': 20,
    'C10': 100000,
    'C11': 10000,
    'C12': 5000000,
    'C13': 40000,
    'C14': 100,
    'C15': 100,
    'C16': 3000000,
    'C17': 50,
    'C18': 10000,
    'C19': 4000,
    'C20': 20,
    'C21': 4000000,
    'C22': 100,
    'C23': 100,
    'C24': 250000,
    'C25': 400,
    'C26': 100000
}


def add_layer_summary(value, tag):
    tf.summary.scalar('%s/fraction_of_zero_values' % tag,
                      tf.nn.zero_fraction(value))
    tf.summary.histogram('%s/activation' % tag, value)


def generate_input_data(filename, batch_size, num_epochs):
    def parse_csv(value):
        tf.logging.info('Parsing {}'.format(filename))
        cont_defaults = [[0.0] for i in range(1, 14)]
        cate_defaults = [[" "] for i in range(1, 27)]
        label_defaults = [[0]]
        column_headers = TRAIN_DATA_COLUMNS
        record_defaults = label_defaults + cont_defaults + cate_defaults
        columns = tf.io.decode_csv(value, record_defaults=record_defaults)
        all_columns = collections.OrderedDict(zip(column_headers, columns))
        labels = all_columns.pop(LABEL_COLUMN[0])
        features = all_columns
        return features, labels

    # Extract lines from input files using the Dataset API.
    dataset = tf.data.TextLineDataset(filename)
    dataset = dataset.shuffle(buffer_size=20000,
                              seed=2021)  # set seed for reproducing
    dataset = dataset.repeat(num_epochs)
    dataset = dataset.prefetch(batch_size)
    dataset = dataset.batch(batch_size)
    dataset = dataset.map(parse_csv, num_parallel_calls=28)
    dataset = dataset.prefetch(1)
    return dataset


def build_feature_cols():
    wide_column = []
    deep_column = []
    fm_column = []
    for column_name in FEATURE_COLUMNS:
        if column_name in CATEGORICAL_COLUMNS:
            categorical_column = tf.feature_column.categorical_column_with_embedding(
                column_name,
                hash_bucket_size=10000,
                dtype=tf.string)

            categorical_embedding_column = tf.feature_column.embedding_column(
                categorical_column, dimension=16, combiner='mean')

            wide_column.append(categorical_embedding_column)
            deep_column.append(categorical_embedding_column)
            fm_column.append(categorical_embedding_column)
        else:
            column = tf.feature_column.numeric_column(column_name, shape=(1, ))
            wide_column.append(column)
            deep_column.append(column)

    return wide_column, fm_column, deep_column


class DeepFM():
    def __init__(self,
                 wide_column=None,
                 fm_column=None,
                 deep_column=None,
                 dnn_hidden_units=[1024, 256, 32],
                 final_hidden_units=[128, 64],
                 optimizer_type='adam',
                 learning_rate=0.001,
                 inputs=None,
                 use_bn=True,
                 bf16=False,
                 input_layer_partitioner=None,
                 dense_layer_partitioner=None):
        if not inputs:
            raise ValueError('Dataset is not defined.')
        self.wide_column = wide_column
        self.deep_column = deep_column
        self.fm_column = fm_column
        if not wide_column or not fm_column or not deep_column:
            raise ValueError(
                'Wide column, FM column or Deep column is not defined.')
        self.dnn_hidden_units = dnn_hidden_units
        self.final_hidden_units = final_hidden_units
        self.optimizer_type = optimizer_type
        self.learning_rate = learning_rate
        self.input_layer_partitioner = input_layer_partitioner
        self.dense_layer_partitioner = dense_layer_partitioner

        self.feature = inputs[0]
        self.label = inputs[1]
        self.bf16 = bf16

        self._is_training = True
        self.use_bn = use_bn

        self.predict = self.prediction()
        with tf.name_scope('head'):
            self.train_op, self.loss = self.optimizer()
            self.acc, self.acc_op = tf.metrics.accuracy(labels=self.label,
                                                        predictions=self.predict)
            self.auc, self.auc_op = tf.metrics.auc(labels=self.label,
                                                predictions=self.predict,
                                                num_thresholds=1000)
            tf.summary.scalar('eval_acc', self.acc)
            tf.summary.scalar('eval_auc', self.auc)

    def dnn(self, dnn_input, dnn_hidden_units=None, layer_name=''):
        for layer_id, num_hidden_units in enumerate(dnn_hidden_units):
            with tf.variable_scope(layer_name + "_%d" % layer_id,
                                   partitioner=self.dense_layer_partitioner,
                                   reuse=tf.AUTO_REUSE) as dnn_layer_scope:
                dnn_input = tf.layers.dense(dnn_input,
                                            units=num_hidden_units,
                                            activation=tf.nn.relu,
                                            name=dnn_layer_scope)
                if self.use_bn:
                    dnn_input = tf.layers.batch_normalization(
                        dnn_input, training=self._is_training, trainable=True)
                add_layer_summary(dnn_input, dnn_layer_scope.name)

        return dnn_input

    def prediction(self):
        # input features
        with tf.variable_scope('input_layer',
                               partitioner=self.input_layer_partitioner,
                               reuse=tf.AUTO_REUSE):

            fm_cols = {}
            wide_input = tf.feature_column.input_layer(
                self.feature, self.wide_column, cols_to_output_tensors=fm_cols)
            fm_input = tf.stack([fm_cols[cols] for cols in self.fm_column], 1)
            dnn_input = tf.feature_column.input_layer(self.feature,
                                                      self.deep_column)

        if self.bf16:
            wide_input = tf.cast(wide_input, dtype=tf.bfloat16)
            fm_input = tf.cast(fm_input, dtype=tf.bfloat16)
            dnn_input = tf.cast(dnn_input, dtype=tf.bfloat16)

        # DNN part
        if self.bf16:
            with tf.variable_scope('dnn').keep_weights():
                dnn_output = self.dnn(dnn_input, self.dnn_hidden_units,
                                      'dnn_layer')
        else:
            with tf.variable_scope('dnn'):
                dnn_output = self.dnn(dnn_input, self.dnn_hidden_units,
                                      'dnn_layer')

        # linear / fisrt order part
        with tf.variable_scope('linear', reuse=tf.AUTO_REUSE) as linear:
            linear_output = tf.reduce_sum(wide_input, axis=1, keepdims=True)

        # FM second order part
        with tf.variable_scope('fm', reuse=tf.AUTO_REUSE) as fm:
            sum_square = tf.square(tf.reduce_sum(fm_input, axis=1))
            square_sum = tf.reduce_sum(tf.square(fm_input), axis=1)
            fm_output = 0.5 * tf.subtract(sum_square, square_sum)

        # Final dnn layer
        all_input = tf.concat([dnn_output, linear_output, fm_output], 1)
        if self.bf16:
            with tf.variable_scope('final_dnn').keep_weights():
                net = self.dnn(all_input, self.final_hidden_units, 'final_dnn')
            net = tf.cast(net, dtype=tf.float32)
        else:
            with tf.variable_scope('final_dnn'):
                net = self.dnn(all_input, self.final_hidden_units, 'final_dnn')

        net = tf.layers.dense(net, units=1)
        net = tf.math.sigmoid(net)

        return net

    def optimizer(self):
        loss_func = tf.losses.mean_squared_error
        self.predict = tf.squeeze(self.predict)
        loss = tf.math.reduce_mean(loss_func(self.label, self.predict))
        tf.summary.scalar('loss', loss)

        self.global_step = tf.train.get_or_create_global_step()
        if self.optimizer_type == 'adam':
            optimizer = tf.train.AdamOptimizer(
                learning_rate=self.learning_rate,
                beta1=0.9,
                beta2=0.999,
                epsilon=1e-8)
        elif self.optimizer_type == 'adagrad':
            optimizer = tf.train.AdagradOptimizer(
                learning_rate=self.learning_rate,
                initial_accumulator_value=1e-8)
        elif self.optimizer_type == 'adamasync':
            optimizer = tf.train.AdamAsyncOptimizer(
                learning_rate=self.learning_rate,
                beta1=0.9,
                beta2=0.999,
                epsilon=1e-8)
        else:
            raise ValueError('Optimzier type error.')

        update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
        with tf.control_dependencies(update_ops):
            train_op = optimizer.minimize(loss, global_step=self.global_step)

        return train_op, loss


def get_arg_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data_location',
                        help='Full path of train data',
                        required=False,
                        default='./data')
    parser.add_argument('--steps',
                        help='set the number of steps on train dataset',
                        type=int,
                        default=0)
    parser.add_argument('--batch_size',
                        help='Batch size to train. Default is 512',
                        type=int,
                        default=512)
    parser.add_argument('--output_dir',
                        help='Full path to logs & model output directory',
                        required=False,
                        default='./result')
    parser.add_argument('--checkpoint',
                        help='Full path to checkpoints input/output directory',
                        required=False)
    parser.add_argument('--deep_dropout',
                        help='Dropout regularization for deep model',
                        type=float,
                        default=0.0)
    parser.add_argument("--optimizer",
                        type=str,
                        choices=["adam", "adagrad", "adamasync"],
                        default="adam")
    parser.add_argument('--learning_rate',
                        help='Learning rate for model',
                        type=float,
                        default=0.001)
    parser.add_argument('--save_steps',
                        help='set the number of steps on saving checkpoints',
                        type=int,
                        default=0)
    parser.add_argument('--keep_checkpoint_max',
                        help='Maximum number of recent checkpoint to keep',
                        type=int,
                        default=1)
    parser.add_argument('--bf16',
                        help='enable DeepRec BF16 in deep model. Default FP32',
                        action='store_true')
    parser.add_argument('--no_eval',
                        help='not evaluate trained model by eval dataset.',
                        action='store_true')
    parser.add_argument('--timeline',
                        help='number of steps on saving timeline. Default 0',
                        type=int,
                        default=0)
    parser.add_argument("--protocol",
                        type=str,
                        choices=["grpc", "grpc++", "star_server"],
                        default="grpc")
    parser.add_argument('--inter',
                        help='set inter op parallelism threads.',
                        type=int,
                        default=0)
    parser.add_argument('--intra',
                        help='set inter op parallelism threads.',
                        type=int,
                        default=0)
    parser.add_argument('--input_layer_partitioner',
                        help='slice size of input layer partitioner. units MB',
                        type=int,
                        default=0)
    parser.add_argument('--dense_layer_partitioner',
                        help='slice size of dense layer partitioner. units KB',
                        type=int,
                        default=0)
    return parser


def main(tf_config=None, server=None):
    # check dataset
    print('Checking dataset')
    train_file = args.data_location + '/train.csv'
    test_file = args.data_location + '/eval.csv'

    if (not os.path.exists(train_file)) or (not os.path.exists(test_file)):
        print(
            '------------------------------------------------------------------------------------------'
        )
        print(
            "train.csv or eval.csv does not exist in the given data_location. Please provide valid path"
        )
        print(
            '------------------------------------------------------------------------------------------'
        )
        sys.exit()
    no_of_training_examples = sum(1 for line in open(train_file))
    no_of_test_examples = sum(1 for line in open(test_file))
    print("Numbers of training dataset is {}".format(no_of_training_examples))
    print("Numbers of test dataset is {}".format(no_of_test_examples))

    # set params
    # set batch size & steps
    batch_size = args.batch_size
    if args.steps == 0:
        no_of_epochs = 10
        train_steps = math.ceil(
            (float(no_of_epochs) * no_of_training_examples) / batch_size)
    else:
        no_of_epochs = math.ceil(
            (float(batch_size) * args.steps) / no_of_training_examples)
        train_steps = args.steps
    test_steps = math.ceil(float(no_of_test_examples) / batch_size)

    # set fixed random seed
    tf.set_random_seed(2021)

    # set directory path
    model_dir = os.path.join(args.output_dir,
                             'model_DeepFM_' + str(int(time.time())))
    checkpoint_dir = args.checkpoint if args.checkpoint else model_dir
    print("Saving model checkpoints to " + checkpoint_dir)

    # create data pipline
    wide_column, fm_column, deep_column = build_feature_cols()
    train_dataset = generate_input_data(train_file, batch_size, no_of_epochs)
    test_dataset = generate_input_data(test_file, batch_size, 1)

    iterator = tf.data.Iterator.from_structure(train_dataset.output_types,
                                               test_dataset.output_shapes)
    next_element = iterator.get_next()

    train_init_op = iterator.make_initializer(train_dataset)
    test_init_op = iterator.make_initializer(test_dataset)

    # create variable partitioner for distributed training
    num_ps_replicas = len(tf_config['ps_hosts']) if tf_config else 0
    input_layer_partitioner = partitioned_variables.min_max_variable_partitioner(
        max_partitions=num_ps_replicas,
        min_slice_size=args.input_layer_partitioner <<
        20) if args.input_layer_partitioner else None
    dense_layer_partitioner = partitioned_variables.min_max_variable_partitioner(
        max_partitions=num_ps_replicas,
        min_slice_size=args.dense_layer_partitioner <<
        10) if args.dense_layer_partitioner else None

    # create model
    model = DeepFM(wide_column=wide_column,
                   fm_column=fm_column,
                   deep_column=deep_column,
                   optimizer_type=args.optimizer,
                   learning_rate=args.learning_rate,
                   bf16=args.bf16,
                   inputs=next_element,
                   input_layer_partitioner=input_layer_partitioner,
                   dense_layer_partitioner=dense_layer_partitioner)

    sess_config = tf.ConfigProto()
    if args.inter:
        sess_config.inter_op_parallelism_threads = args.inter
    if args.intra:
        sess_config.intra_op_parallelism_threads = args.intra
        hooks = []

    if tf_config:
        print('train steps : %d' % train_steps)
        hooks.append(tf.train.StopAtStepHook(last_step=train_steps))
        hooks.append(
            tf.train.LoggingTensorHook(
                {
                    'steps': model.global_step,
                    'loss': model.loss
                },
                every_n_iter=100))

        scaffold = tf.train.Scaffold(
            local_init_op=tf.group(tf.global_variables_initializer(
            ), tf.local_variables_initializer(), train_init_op))

        with tf.train.MonitoredTrainingSession(
                master=server.target,
                is_chief=tf_config['is_chief'],
                checkpoint_dir=checkpoint_dir,
                scaffold=scaffold,
                hooks=hooks,
                # save_checkpoint_steps=args.save_steps,
                log_step_count_steps=100,
                config=sess_config) as sess:
            while not sess.should_stop():
                _, train_loss = sess.run([model.train_op, model.loss])
    else:
        with tf.Session(config=sess_config) as sess:
            sess.run(tf.global_variables_initializer())
            sess.run(tf.local_variables_initializer())
            merged = tf.summary.merge_all()
            writer = tf.summary.FileWriter(checkpoint_dir, sess.graph)
            saver = tf.train.Saver(tf.global_variables(),
                                   max_to_keep=args.keep_checkpoint_max)
            options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()

            # train model
            sess.run(train_init_op)
            model._is_training = True

            start = time.perf_counter()
            for _in in range(0, train_steps):
                if args.save_steps > 0 and (_in % args.save_steps == 0
                                            or _in == train_steps - 1):
                    _, train_loss, events = sess.run(
                        [model.train_op, model.loss, merged])
                    writer.add_summary(events, _in)
                    checkpoint_path = saver.save(sess,
                                                 save_path=os.path.join(
                                                     checkpoint_dir,
                                                     'DeepFM-checkpoint'),
                                                 global_step=_in)
                    print("Save checkpoint to %s" % checkpoint_path)
                elif (args.timeline > 0 and _in % args.timeline == 0):
                    _, train_loss = sess.run([model.train_op, model.loss],
                                             options=options,
                                             run_metadata=run_metadata)
                    fetched_timeline = timeline.Timeline(
                        run_metadata.step_stats)
                    chrome_trace = fetched_timeline.generate_chrome_trace_format(
                    )
                    print("Save timeline to %s" % checkpoint_dir)
                    with open(
                            os.path.join(checkpoint_dir,
                                         'timeline-%d.json' % _in), 'w') as f:
                        f.write(chrome_trace)
                else:
                    _, train_loss = sess.run([model.train_op, model.loss])

                # print training loss and time cost
                if (_in % 100 == 0 or _in == train_steps - 1):
                    end = time.perf_counter()
                    cost_time = end - start
                    global_step_sec = (100 if _in % 100 == 0 else train_steps -
                                       1 % 100) / cost_time
                    print("global_step/sec: %0.4f" % global_step_sec)
                    print("loss = {}, steps = {}, cost time = {:0.2f}s".format(
                        train_loss, _in, cost_time))
                    start = time.perf_counter()

            # eval model
            if not args.no_eval:
                writer = tf.summary.FileWriter(
                    os.path.join(checkpoint_dir, 'eval'))

                sess.run(test_init_op)
                sess.run(tf.local_variables_initializer())
                model._is_training = False

                for _in in range(1, test_steps + 1):
                    if (_in != test_steps):
                        sess.run(
                            [model.acc, model.acc_op, model.auc, model.auc_op])
                        if (_in % 1000 == 0):
                            print("Evaluation complate:[{}/{}]".format(
                                _in, test_steps))
                    else:
                        _, eval_acc, _, eval_auc, events = sess.run([
                            model.acc, model.acc_op, model.auc, model.auc_op,
                            merged
                        ])
                        writer.add_summary(events, _in)
                        print("Evaluation complate:[{}/{}]".format(
                            _in, test_steps))
                        print("ACC = {}\nAUC = {}".format(eval_acc, eval_auc))


if __name__ == "__main__":
    parser = get_arg_parser()
    args = parser.parse_args()

    TF_CONFIG = os.getenv('TF_CONFIG')
    if not TF_CONFIG:
        main()
    else:
        print(TF_CONFIG)
        tf_config = json.loads(TF_CONFIG)
        cluster_config = tf_config.get('cluster')
        ps_hosts = []
        worker_hosts = []
        chief_hosts = []
        for key, value in cluster_config.items():
            if "ps" == key:
                ps_hosts = value
            elif "worker" == key:
                worker_hosts = value
            elif "chief" == key:
                chief_hosts = value
        if chief_hosts:
            worker_hosts = chief_hosts + worker_hosts

        if not ps_hosts or not worker_hosts:
            print('TF_CONFIG ERROR')
            sys.exit()
        task_config = tf_config.get('task')
        task_type = task_config.get('type')
        task_index = task_config.get('index') + (1 if task_type == 'worker'
                                                 and chief_hosts else 0)

        if task_type == 'chief':
            task_type = 'worker'

        is_chief = True if task_index == 0 else False
        cluster = tf.train.ClusterSpec({
            "ps": ps_hosts,
            "worker": worker_hosts
        })
        server = tf.distribute.Server(cluster,
                                      job_name=task_type,
                                      task_index=task_index,
                                      protocol=args.protocol)
        if task_type == 'ps':
            server.join()
        elif task_type == 'worker':
            with tf.device(
                    tf.train.replica_device_setter(
                        worker_device="/job:worker/task:%d" % task_index,
                        cluster=cluster)):
                main(tf_config={
                    'ps_hosts': ps_hosts,
                    'worker_hosts': worker_hosts,
                    'type': task_type,
                    'index': task_index,
                    'is_chief': is_chief
                },
                     server=server)
        else:
            print("Task type or index error.")
            sys.exit()