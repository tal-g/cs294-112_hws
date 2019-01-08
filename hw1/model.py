import os
import sys
import logging
import time
import gym
import numpy as np
import tensorflow as tf

import tf_util

logging.basicConfig(level=logging.INFO)


def get_batch_generator(data, batch_size, shuffle=False):
    n = data['observations'].shape[0]
    if shuffle:
        indices = np.random.permutation(n)
        data = {'observations': data['observations'][indices], 'actions': data['actions'][indices]}
    for i in range(0, n, batch_size):
        yield {'observations':data['observations'][i:i + batch_size], 'actions': data['actions'][i:i + batch_size]}

        
def write_summary(value, tag, summary_writer, global_step):
    """Write a single summary value to tensorboard"""
    summary = tf.Summary()
    summary.value.add(tag=tag, simple_value=value)
    summary_writer.add_summary(summary, global_step)

    
class Model:
    '''Top-level model'''
    
    def __init__(self, FLAGS, algorithm='behavioral_cloning'):
        print('Initializing the model...')
        self.FLAGS = FLAGS
        self.algorithm = algorithm
        
        with tf.variable_scope(
            'model', 
            initializer=tf.keras.initializers.he_normal(), 
            regularizer=tf.contrib.layers.l2_regularizer(scale=3e-7), 
            reuse=tf.AUTO_REUSE
        ):
            self.add_placeholders()
            self.build_graph()
            self.add_loss()
            
        params = tf.trainable_variables()
        gradients = tf.gradients(self.loss, params)
        self.gradient_norm = tf.global_norm(gradients)
        clipped_gradients, _ = tf.clip_by_global_norm(gradients, self.FLAGS['max_gradient_norm'])
        self.param_norm = tf.global_norm(params)
        
        self.global_step = tf.Variable(0, name="global_step", trainable=False)
        lr = self.FLAGS['learning_rate']
        opt = tf.train.AdamOptimizer(learning_rate=lr, beta1=0.8, beta2=0.999, epsilon=1e-7)
        self.updates = opt.apply_gradients(zip(clipped_gradients, params), global_step=self.global_step)
        
        self.saver = tf.train.Saver(tf.global_variables(), max_to_keep=1)
        self.bestmodel_saver = tf.train.Saver(tf.global_variables(), max_to_keep=1)
        self.summaries = tf.summary.merge_all()

    
    def add_placeholders(self):
        self.x = tf.placeholder(tf.float32, shape=[None, self.FLAGS['input_dim']])
        self.y = tf.placeholder(tf.float32, shape=[None, 1, self.FLAGS['output_dim']])
        
        self.keep_prob = tf.placeholder_with_default(1.0, shape=())
        
        
    def build_graph(self):
        out = tf.contrib.layers.fully_connected(self.x, self.FLAGS['hidden_dims'][0], tf.nn.relu, scope='h0')
        for i in range(1, len(self.FLAGS['hidden_dims'])):
            out = tf.contrib.layers.fully_connected(out, self.FLAGS['hidden_dims'][i], tf.nn.relu, scope='h{}'.format(i))
            out = tf.nn.dropout(out, self.keep_prob)
        out = tf.contrib.layers.fully_connected(out, self.FLAGS['output_dim'], activation_fn=None, scope='final')
        self.out = tf.expand_dims(out, axis=1)
    
    
    def add_loss(self):
        with tf.variable_scope('loss'):
            if self.FLAGS['loss'] == 'l2_loss':
                self.loss = tf.reduce_mean(tf.reduce_sum((self.y - self.out) ** 2, axis=-1))
            tf.summary.scalar('loss', self.loss)
    
    
    def run_train_iter(self, session, batch, summary_writer):
        input_feed = dict()
        input_feed[self.x] = batch['observations']
        input_feed[self.y] = batch['actions']
        input_feed[self.keep_prob] = 1.0 - self.FLAGS['dropout']
        
        output_feed = [self.updates, self.summaries, self.loss, self.global_step, self.param_norm, self.gradient_norm]
        
        [_, summaries, loss, global_step, param_norm, gradient_norm] = session.run(output_feed, input_feed)
        
        summary_writer.add_summary(summaries, global_step)
        
        return loss, global_step, param_norm, gradient_norm
        
    
    def get_loss(self, session, batch):
        input_feed = dict()
        input_feed[self.x] = batch['observations']
        input_feed[self.y] = batch['actions']
        
        output_feed = [self.loss]
        
        [loss] = session.run(output_feed, input_feed)
        
        return loss
    
    
    def get_predictions(self, session, observations):
        input_feed = dict()
        input_feed[self.x] = observations
        
        output_feed = [self.out]
        
        [out] = session.run(output_feed, input_feed)
        
        return out
    
    
    def get_val_loss(self, session, data_val):
        total_loss, num_samples = 0, 0
        for batch in get_batch_generator(data_val, self.FLAGS['batch_size'], shuffle=False):
            loss = self.get_loss(session, batch)
            curr_batch_size = batch['observations'].shape[0]
            total_loss += (loss * curr_batch_size)
            num_samples += curr_batch_size
        val_loss = total_loss / num_samples
        return val_loss

    
    def evaluate(self, session, env, num_rollouts, max_steps):
        returns = []
        observations = []
        for i in range(num_rollouts):
            obs = env.reset()
            done = False
            total = steps = 0
            while not done:
                action = self.get_predictions(session, obs[None, :])
                observations.append(obs)
                obs, r, done, _ = env.step(action)
                total += r
                steps += 1
                if steps >= max_steps:
                    break
            returns.append(total)
        return returns, observations
        
    
    def train(self, session, curr_dir, bestmodel_dir, data_train, data_val):
        env = gym.make(self.FLAGS['env_name'])
        num_rollouts = self.FLAGS['num_rollouts']
        max_steps = self.FLAGS['max_timesteps'] or env.spec.timestep_limit
        
        params = tf.trainable_variables()
        num_params = sum(map(lambda t: np.prod(tf.shape(t.value()).eval()), params))
        logging.info('Number of params: {}'.format(num_params))
        
        exp_loss = None
        
        checkpoint_path = os.path.join(curr_dir, 'm.ckpt')
        bestmodel_ckpt_path = os.path.join(bestmodel_dir, 'm_best.ckpt')
        self.best_return = None
        
        summary_writer = tf.summary.FileWriter(curr_dir, session.graph)
        
        epoch = 0
        
        self.returns = []
        
        logging.info('Beginning training loop...')
        while self.FLAGS['num_epochs'] == 0 or epoch < self.FLAGS['num_epochs']:
            epoch += 1
            epoch_tic = time.time()
            
            for batch in get_batch_generator(data_train, self.FLAGS['batch_size'], shuffle=True):
                loss, global_step, param_norm, grad_norm = self.run_train_iter(session, batch, summary_writer)
                if not exp_loss:
                    exp_loss = loss
                else:
                    exp_loss = 0.99 * exp_loss + 0.01 * loss
            
            if epoch % self.FLAGS['eval_every'] != 0:
                continue
            
            val_loss = self.get_val_loss(session, data_val)
            
            write_summary(val_loss, 'val/loss', summary_writer, global_step)
            
            logging.info(
                'epoch {}, iter {}, loss {:.5f}, smoothed loss {:.5f}, grad norm {:.5f}, param norm {:.5f}, val loss {:.5f}'.\
                format(epoch, global_step, loss, exp_loss, grad_norm, param_norm, val_loss)
            )
            
            curr_returns, curr_observations = self.evaluate(session, env, num_rollouts, max_steps)
            self.returns.append(curr_returns)
            
            logging.info(
                'epoch {}, iter {}, mean return {}, std of return {}'.\
                format(epoch, global_step, np.mean(curr_returns), np.std(curr_returns))
            )
            
            if self.best_return is None or np.mean(curr_returns) > self.best_return:
                self.best_return, self.best_return_std = np.mean(curr_returns), np.std(curr_returns)
                logging.info('Saving to {} ...'.format(bestmodel_ckpt_path))
                self.bestmodel_saver.save(session, bestmodel_ckpt_path, global_step=global_step)
            
            epoch_toc = time.time()
            logging.info('End of epoch {}. Time for epoch: {}'.format(epoch, epoch_toc - epoch_tic))
        
        logging.info('Saving to {} ...'.format(checkpoint_path))
        self.saver.save(session, checkpoint_path, global_step=global_step)
        
        logging.info('best: mean return {}, std of return {}'.format(self.best_return, self.best_return_std))
        sys.stdout.flush()            