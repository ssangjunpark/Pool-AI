import gymnasium as gym
from gymnasium.wrappers import RecordVideo
import tensorflow as tf
from tensorflow.keras import layers
import matplotlib.pyplot as plt
import numpy as np
from datetime import datetime

RESIZE_FACTOR = 2

class ReplayMemoryBuffer:
    def __init__(self, size, observation_shape, action_space_dim):
        self.counter = 0
        self.pointer = 0
        self.max_size = size

        self.frames = np.empty((size, *observation_shape), dtype=np.float32)
        self.actions = np.empty((size, action_space_dim), dtype=np.float32)
        self.rewards = np.empty(size, dtype=np.float32)
        self.terminal = np.empty(size, dtype=np.bool_)

    def store(self, frame, action, reward, terminal):
        self.frames[self.pointer] = frame
        self.actions[self.pointer] = action
        self.rewards[self.pointer] = reward
        self.terminal[self.pointer] = terminal

        self.counter = max(self.counter, self.pointer + 1)
        self.pointer = (self.pointer + 1) % self.max_size

    def sample_batch(self, batch_size):
        indexes = np.random.randint(low=0, high=self.counter - 1, size=batch_size)
        batch = dict(
            s   = self.frames[indexes],
            a   = self.actions[indexes],
            # i will not shift r since we start with r_t+1 anyways so gotta assume offset -1 :(
            r   = self.rewards[indexes],
            s2  = self.frames[indexes + 1],
            d   = self.terminal[indexes]
        )
        batch['s']  = tf.convert_to_tensor(batch['s'], dtype=tf.float32)
        batch['a'] = tf.convert_to_tensor(batch['a'], dtype=tf.float32)
        batch['r']  = tf.convert_to_tensor(batch['r'], dtype=tf.float32)
        batch['s2']  = tf.convert_to_tensor(batch['s2'], dtype=tf.float32)
        batch['d']  = tf.convert_to_tensor(batch['d'], dtype=tf.float32)
        
        return batch


class QValue(tf.keras.Model):
    def __init__(self, conv_sizes, dense_sizes):
        super().__init__()

        self.conv_layers = []
        for filters, kernal_size, strides in conv_sizes:
            self.conv_layers.append(
                layers.Conv2D(filters=filters, kernel_size=kernal_size, strides=strides, activation='relu')
            )
        
        self.flatten = layers.Flatten()

        self.fc_layers = []
        for size in dense_sizes:
            self.fc_layers.append(
                layers.Dense(size, activation='relu')
            )
        
        self.final_q = layers.Dense(1, activation=None)
    
    def call(self, state, action):
        # i dont know how they want me to use the action so im just going to concat after conv 
        # TODO: experiment with how to use the action 
        x = state
        for conv in self.conv_layers:
            x = conv(x)

        x = self.flatten(x)

        x = tf.concat([x,action], axis=-1)

        for fc in self.fc_layers:
            x = fc(x)
        
        final_q = self.final_q(x)

        return final_q
    
class PolicyPi(tf.keras.Model):
    # details about reparameterization, tanh squashing and gaussian log
    # https://spinningup.openai.com/en/latest/algorithms/sac.html
    # https://en.wikipedia.org/wiki/Log-normal_distribution

    def __init__(self, conv_sizes, dense_sizes, action_space_dim):
        super().__init__()

        self.conv_layers = []
        for filters, kernal_size, strides in conv_sizes:
            self.conv_layers.append(
                layers.Conv2D(filters=filters, kernel_size=kernal_size, strides=strides, activation='relu')
            )
        
        self.flatten = layers.Flatten()

        self.fc_layers = []
        for size in dense_sizes:
            self.fc_layers.append(
                layers.Dense(size, activation='relu')
            )
        
        self.mu = layers.Dense(action_space_dim, activation=None)

        # will later be expontinated to get the regular std value to ensure +ve
        self.log_std = layers.Dense(action_space_dim, activation=None)

    def call(self, state):
        x = state
        for conv in self.conv_layers:
            x = conv(x)
        
        x = self.flatten(x)

        for fc in self.fc_layers:
            x = fc(x)
        
        mu = self.mu(x)
        log_std = self.log_std(x)

        log_std = tf.clip_by_value(log_std, -20, 2)

        std = tf.exp(log_std)
        
        return mu, std

    def sample_action(self, state):
        mu, std = self(state)

        # reparameterization trick with squished gaussian (tanh) to sample action
        xi = tf.random.normal(tf.shape(mu))
        a_bar = mu + std * xi
        action = tf.tanh(a_bar)

        # log likelihood of standard gaussian and dealing case divide by 0
        # https://www.statlect.com/fundamentals-of-statistics/normal-distribution-maximum-likelihood 
        # dropped constnats since will be part of lr anyways
        log_gaussian = -0.5 * ( ( (a_bar - mu)/(std+1e-6) )**2 + 2*tf.math.log(std+1e-6) + np.log(2*np.pi)  )

        log_gaussian = tf.reduce_sum(log_gaussian, axis=1, keepdims=True)

        # seemse like correction is required after reparameterization
        # https://github.com/haarnoja/sac/blob/master/sac/policies/gaussian_policy.py#L74
        log_squash = tf.reduce_sum( tf.math.log(1 - action**2 + 1e-6), axis=1, keepdims=True )

        log_prob = log_gaussian - log_squash

        return action, log_prob
    

class SAC:
    def __init__(self, observation_shape, action_space_dim, gamma=0.99, polyak=0.995, alpha=0.2, Q_lr=3e-4, pi_lr=3e-4):
        # observation_shape_new = []
        # observation_shape_new.append(observation_shape[0] // RESIZE_FACTOR)
        # observation_shape_new.append(observation_shape[1] // RESIZE_FACTOR)
        # observation_shape_new.append(observation_shape[2])
        # print(observation_shape_new.shape)

        self.replayMemoryBuffer = ReplayMemoryBuffer(1000000, observation_shape, action_space_dim)
        
        self.gamma = gamma
        self.polyak = polyak
        self.alpha = alpha
        self.Q_lr = Q_lr
        self.pi_lr = pi_lr

        # we need four Q in total - two for phi and two for phi target
        # self.Q1 = QValue(conv_sizes=[[32,8,4], [64,4,3], [64,3,1]], dense_sizes=[256])
        # self.Q2 = QValue(conv_sizes=[[32,8,4], [64,4,3], [64,3,1]], dense_sizes=[256])
        # self.Q1_target = QValue(conv_sizes=[[32,8,4], [64,4,3], [64,3,1]], dense_sizes=[256])
        # self.Q2_target = QValue(conv_sizes=[[32,8,4], [64,4,3], [64,3,1]], dense_sizes=[256])
        # self.pi = PolicyPi(conv_sizes=[[32,8,4], [64,4,3], [64,3,1]], dense_sizes=[256], action_space_dim=action_space_dim)

        self.Q1 = QValue(conv_sizes=[], dense_sizes=[256, 256])
        self.Q2 = QValue(conv_sizes=[], dense_sizes=[256, 256])
        self.Q1_target = QValue(conv_sizes=[], dense_sizes=[256, 256])
        self.Q2_target = QValue(conv_sizes=[], dense_sizes=[256, 256])
        self.pi = PolicyPi(conv_sizes=[], dense_sizes=[256, 256], action_space_dim=action_space_dim)


        self.Q1_target.set_weights(self.Q1.get_weights())
        self.Q2_target.set_weights(self.Q2.get_weights())

        self.Q1_optim = tf.keras.optimizers.Adam(self.Q_lr)
        self.Q2_optim = tf.keras.optimizers.Adam(self.Q_lr)
        self.pi_optim = tf.keras.optimizers.Adam(self.pi_lr)

    @tf.function
    def update(self, batch_size=256):
        batch = self.replayMemoryBuffer.sample_batch(batch_size)
        s, a, r, s2, d = batch['s'], batch['a'], batch['r'], batch['s2'], batch['d']

        with tf.GradientTape(persistent=True) as tape:
            a2, lpi2 = self.pi.sample_action(s2)
            q_1_target_v = self.Q1_target(s2, a2)
            q_2_target_v = self.Q2_target(s2, a2)
            
            compare_q_target = tf.minimum(q_1_target_v, q_2_target_v)

            y = r[:, None] + self.gamma * (1 - d[:, None]) * (compare_q_target - self.alpha * lpi2)

            q_1_v = self.Q1(s, a)
            q_2_v = self.Q2(s, a)

            q_1_loss = tf.reduce_sum((q_1_v - y) ** 2)
            q_2_loss = tf.reduce_sum((q_2_v - y) ** 2)

        q_1_grad = tape.gradient(q_1_loss, self.Q1.trainable_variables)
        q_2_grad = tape.gradient(q_2_loss, self.Q2.trainable_variables)
        self.Q1_optim.apply_gradients(zip(q_1_grad, self.Q1.trainable_variables))
        self.Q2_optim.apply_gradients(zip(q_2_grad, self.Q2.trainable_variables))


        with tf.GradientTape(persistent=True) as tape:
            a1, lpi1 = self.pi.sample_action(s)

            q_1_a1_v = self.Q1(s, a1)
            q_2_a1_v = self.Q2(s, a1)

            compare_q_a1_v = tf.minimum(q_1_a1_v, q_2_a1_v)

            # gradient ascent thus negative sign 
            pi_loss = -tf.reduce_sum(compare_q_a1_v - self.alpha * lpi1)
        
        pi_grad = tape.gradient(pi_loss, self.pi.trainable_variables)
        self.pi_optim.apply_gradients(zip(pi_grad, self.pi.trainable_variables))

        del tape

        for weight_target, weight in zip(self.Q1_target.trainable_variables, self.Q1.trainable_variables):
            new_phi = self.polyak * weight_target + (1 - self.polyak) * weight
            
            weight_target.assign(new_phi)

        for weight_target, weight in zip(self.Q2_target.trainable_variables, self.Q2.trainable_variables):
            new_phi = self.polyak * weight_target + (1 - self.polyak) * weight
            
            weight_target.assign(new_phi)

        return q_1_loss, q_2_loss, pi_loss
    

def train(env):
    observation_shape = env.observation_space.shape
    action_space_dim = env.action_space.shape[0]

    agent = SAC(observation_shape, action_space_dim)

    init_steps = 10000
    # init_steps = 300
    init_steps_counter = 0
    # to fill the buffer (and for stacking if implemenetd later)
    while (init_steps > init_steps_counter):
        observation, _ = env.reset()
        done = False
        while not done:
            action = env.action_space.sample()
            observation2, reward, done, trunc, info = env.step(action)
            agent.replayMemoryBuffer.store(observation, action, reward, done)

            observation = observation2

            init_steps_counter += 1
            if (init_steps_counter % 100 == 0):
                print(f"{init_steps_counter} / {init_steps} filled.")

            if (init_steps <= init_steps_counter):
                print(f"Buffer filled: {init_steps_counter} time steps. Start Training")   
                break

    num_episodes = 1000
    # num_episodes = 3
    update_period_timestep = 1
    total_time_steps = 0

    max_num_steps_per_episode = 4000
    current_episode_time_steps = 0

    rewards = np.zeros(shape=num_episodes)

    for episode in range(num_episodes):
        start_time = datetime.now()
        observation, _ = env.reset()
        ep_reward = 0
        done = False

        while not done:
            action, _ = agent.pi.sample_action(observation[None, ...])

            observation2, reward, done, _, _ = env.step(action.numpy()[0])

            #s_t, a_t, r_t+1
            agent.replayMemoryBuffer.store(observation, action, reward, done)

            # s_t -> s_t+1
            observation = observation2
            ep_reward += reward
            total_time_steps += 1
	        
            current_episode_time_steps += 1

            if (total_time_steps % update_period_timestep == 0):
                agent.update()
           
            if (current_episode_time_steps >= max_num_steps_per_episode):
                break
        
        current_episode_time_steps = 0
        rewards[episode] = ep_reward
        print(f"Episode {episode}, Reward {ep_reward}, Time Taken: {datetime.now() - start_time}, Total Timesteps: {total_time_steps}")

    env.close()
    return rewards

def main():
    env = gym.make('HalfCheetah-v5', render_mode='rgb_array')
    # env = RecordVideo(env, episode_trigger= lambda x : True, video_folder='saves')
    rewards = train(env)

    plt.plot(rewards)
    plt.savefig('results')

if __name__ == "__main__":
    main()

