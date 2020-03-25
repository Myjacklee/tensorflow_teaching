import tensorflow as tf
import numpy as np
import os
import argparse


class Config:
    def __init__(self):
        self.batch_size = 20
        self.num_step1 = 8
        self.num_step2 = 10
        self.num_units = 5

        self.gpus = self.get_gpus()
        self.ch_size = 200
        self.en_size = 100

        self.name = 'p26'
        self.save_path = 'models/{name}/{name}'.format(name=self.name)
        self.logdir = 'logs/{name}/'.format(name=self.name)

        self.lr = 0.0002
        self.epoches = 100

    def get_gpus(self):
        value = os.getenv('CUDA_VISIBLE_DEVICES', '0')
        return len(value.split(','))

    def from_cmd_line(self):
        parser = argparse.ArgumentParser()
        for name in dir(self):
            value = getattr(self, name)
            if type(value) in (int, float, bool, str) and not name.startswith('__'):
                parser.add_argument('--' + name, default=value, help='Default to %s' % value, type=type(value))
        a = parser.parse_args()
        for name in dir(self):
            value = getattr(self, name)
            if type(value) in (int, float, bool, str) and not name.startswith('__') and hasattr(a, name):
                value = getattr(a, name)
                setattr(self, name, value)


class Tensors:
    def __init__(self, config: Config):
        self.config = config
        self.sub_ts = []

        with tf.device('/gpu:0'):
            self.lr = tf.placeholder(tf.float32, [], 'lr')
            opt = tf.train.AdamOptimizer(self.lr)

        with tf.variable_scope('poem'):
            for gpu_index in range(config.gpus):
                self.sub_ts.append(SubTensors(config, gpu_index, opt))
                tf.get_variable_scope().reuse_variables()

        with tf.device('/gpu:0'):
            grad = self.merge_grads()

            self.train_op = opt.apply_gradients(grad)
            self.loss = tf.reduce_mean([ts.loss for ts in self.sub_ts])

            tf.summary.scalar('loss', self.loss)
            self.summary_op = tf.summary.merge_all()

        total = 0
        for var in tf.trainable_variables():
            num = self.get_param_num(var.shape)
            print(var.name, var.shape, num)
            total += num
        print('Total params:', total)

    def get_param_num(self, shape):
        num = 1
        for sh in shape:
            num *= sh.value
        return num

    def merge_grads(self):
        indexed_grads = {}
        grads = {}
        for ts in self.sub_ts:
            for g, v in ts.grad:
                if isinstance(g, tf.IndexedSlices):
                    if not v in indexed_grads:
                        indexed_grads[v] = []
                    indexed_grads[v].append(g)
                else:
                    if not v in grads:
                        grads[v] = []
                    grads[v].append(g)
        # grads = { v1: [g11, g12, g13, ...], v2:[g21, g22, ...], ....}
        grads = [(tf.reduce_mean(grads[v], axis=0), v) for v in grads]

        for v in indexed_grads:
            indices = tf.concat([g.indices for g in indexed_grads[v]], axis=0)
            values = tf.concat([g.values for g in indexed_grads[v]], axis=0)
            g = tf.IndexedSlices(values, indices)
            grads.append((g, v))

        return grads


class SubTensors:
    def __init__(self, config, gpu_index, opt):
        with tf.device('/gpu:%d' % gpu_index):
            self.x = tf.placeholder(tf.int32, [None, config.num_step1], 'x')
            self.y = tf.placeholder(tf.int32, [None, config.num_step2], 'y')

            losses, self.y_predict = self.encode_decode(self.x, self.y, config)
            self.loss = tf.reduce_mean(losses)
            self.grad = opt.compute_gradients(self.loss)

    def encode_decode(self, x, y, config):
        with tf.variable_scope('encode'):
            vector = self.encode(x, config)

        with tf.variable_scope('decode'):
            batch_size_ts = tf.shape(x)[0]
            losses, y_predict = self.decode(vector, batch_size_ts, config)

        return losses, y_predict

    def encode(self, x, config):
        cell1 = tf.nn.rnn_cell.BasicLSTMCell(config.num_units, name='encoder_lstm1')
        cell2 = tf.nn.rnn_cell.BasicLSTMCell(config.num_units, name='encoder_lstm2')
        cell = tf.nn.rnn_cell.MultiRNNCell([cell1, cell2])

        ch_dict = tf.get_variable('ch_dict', [config.ch_size, config.num_units], tf.float32)
        x = tf.nn.embedding_lookup(ch_dict, x)  # [-1, num_step1, num_units]

        batch_size_ts = tf.shape(x)[0]
        state = cell.zero_state(batch_size_ts, tf.float32)
        with tf.variable_scope('rnn'):
            for i in range(config.num_step1):
                xi = x[:, i, :]  # [-1, num_units]
                _, state = cell(xi, state)
                #tf.get_variable_scope().reuse_variables()

        return state  # [-1, num_units]

    def decode(self, state, batch_size_ts, config):
        #  state: [-1, num_units]

        cell1 = tf.nn.rnn_cell.BasicLSTMCell(config.num_units, name='decoder_lstm1')
        cell2 = tf.nn.rnn_cell.BasicLSTMCell(config.num_units, name='decoder_lstm2')
        cell = tf.nn.rnn_cell.MultiRNNCell([cell1, cell2])

        with tf.variable_scope('rnn'):
            xi = tf.zeros([batch_size_ts, config.num_units], tf.float32)
            y = tf.one_hot(self.y, config.en_size)  # [-1, num_step2, en_size]

            y_predict = []
            losses = []
            for i in range(config.num_step2):
                yi_predict, state = cell(xi, state)  # [-1, num_units]
                yi = y[:, i, :]  # [-1, en_size]
                # y_predict.append(tf.argmax(yi_predict, axis=1))

                logits = tf.layers.dense(yi_predict, config.en_size, name='dense')  # [-1, en_size]
                y_predict.append(tf.argmax(logits, axis=1))  # ****
                loss = tf.nn.softmax_cross_entropy_with_logits_v2(labels=yi, logits=logits)
                losses.append(loss)

                tf.get_variable_scope().reuse_variables()

        return losses, y_predict


class Samples:
    def __init__(self, config: Config):
        self.config = config
        self.index = 0
        self.chinese = list(np.random.randint(0, config.ch_size, size=[self.num(), config.num_step1]))
        self.english = list(np.random.randint(0, config.en_size, size=[self.num(), config.num_step2]))

    def num(self):
        """
        The number of the samples in total.
        :return:
        """
        return 100

    def next_batch(self, batch_size):
        next = self.index + batch_size
        if next < self.num():
            result0 = self.chinese[self.index: next]
            result1 = self.english[self.index: next]
        else:
            result0 = self.chinese[self.index:]
            result1 = self.english[self.index:]
            next -= self.num()
            result0 += self.chinese[:next]
            result1 += self.english[:next]
        self.index = next
        return result0, result1  # [batch_size, num_step], [batch_size, num_step]


class App:
    def __init__(self, config: Config):
        self.config = config
        graph = tf.Graph()
        with graph.as_default():
            self.samples = Samples(config)
            self.tensors = Tensors(config)
            cfg = tf.ConfigProto()
            cfg.allow_soft_placement = True
            self.session = tf.Session(config=cfg, graph=graph)
            self.saver = tf.train.Saver()
            try:
                self.saver.restore(self.session, config.save_path)
                print('Restore the model from %s successfully.' % config.save_path)
            except:
                print('Fail to restore the model from', config.save_path)
                self.session.run(tf.global_variables_initializer())

    def train(self):
        cfg = self.config
        writer = tf.summary.FileWriter(cfg.logdir, self.session.graph)

        for epoch in range(cfg.epoches):
            batches = self.samples.num() // (cfg.gpus * cfg.batch_size)
            for batch in range(batches):
                feed_dict = {
                    self.tensors.lr: cfg.lr
                }
                for gpu_index in range(cfg.gpus):
                    x, y = self.samples.next_batch(cfg.batch_size)
                    feed_dict[self.tensors.sub_ts[gpu_index].x] = x
                    feed_dict[self.tensors.sub_ts[gpu_index].y] = y
                _, loss, su = self.session.run(
                    [self.tensors.train_op, self.tensors.loss, self.tensors.summary_op], feed_dict)
                writer.add_summary(su, epoch * batches + batch)
                print('%d/%d: loss=%.8f' % (batch, epoch, loss), flush=True)
            self.saver.save(self.session, cfg.save_path)
            print('Save the mode into ', cfg.save_path, flush=True)

    def predict(self):
        pass

    def close(self):
        self.session.close()


if __name__ == '__main__':
    config = Config()
    config.from_cmd_line()

    app = App(config)

    app.train()
    app.close()
    print('Finished!')
