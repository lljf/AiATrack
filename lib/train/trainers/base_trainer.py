import glob
import os
import traceback

import torch
from torch.utils.data.distributed import DistributedSampler

from lib.train.admin import multigpu


class BaseTrainer:
    """
    Base trainer class. Contains functions for training and saving/loading checkpoints.
    Trainer classes should inherit from this one and overload the train_epoch function.
    """

    def __init__(self, actor, loaders, optimizer, settings, lr_scheduler=None):
        """
        Args:
            actor: The actor for training the network.
            loaders: List of dataset loaders, e.g. [train_loader, val_loader].
                     In each epoch, the trainer runs one epoch for each loader.
            optimizer: The optimizer used for training, e.g. Adam.
            settings: Training settings.
            lr_scheduler: Learning rate scheduler.
        """

        self.actor = actor
        self.optimizer = optimizer
        self.lr_scheduler = lr_scheduler
        self.loaders = loaders

        self.update_settings(settings)

        self.epoch = 0
        self.stats = {}

        self.device = getattr(settings, 'device', None)
        if self.device is None:
            self.device = torch.device('cuda:0' if torch.cuda.is_available() and settings.use_gpu else 'cpu')

        self.actor.to(self.device)
        self.settings = settings

    def update_settings(self, settings=None):
        """
        Updates the trainer settings. Must be called to update internal settings.
        """

        if settings is not None:
            self.settings = settings

        if self.settings.env.workspace_dir is not None:
            self.settings.env.workspace_dir = os.path.expanduser(self.settings.env.workspace_dir)
            # New function: specify checkpoint dir
            if self.settings.save_dir is None:
                self._checkpoint_dir = os.path.join(self.settings.env.workspace_dir, 'checkpoints')
            else:
                self._checkpoint_dir = os.path.join(self.settings.save_dir, 'checkpoints')
            # print('checkpoints will be saved to %s' % self._checkpoint_dir)

            if self.settings.local_rank in [-1, 0]:
                if not os.path.exists(self._checkpoint_dir):
                    # print("training with multiple GPUs, checkpoints directory doesn't exist")
                    # print('create checkpoints directory ...')
                    os.makedirs(self._checkpoint_dir)
        else:
            self._checkpoint_dir = None

    def train(self, max_epochs, load_latest=False, fail_safe=True):
        """
        Do training for the given number of epochs.

        Args:
            max_epochs: Max number of training epochs,
            load_latest: Bool indicating whether to resume from latest epoch.
            fail_safe: Bool indicating whether the training to automatically restart in case of any crashes.
        """

        epoch = -1
        num_tries = 1
        for i in range(num_tries):
            try:
                if load_latest:
                    self.load_checkpoint()

                for epoch in range(self.epoch + 1, max_epochs + 1):
                    self.epoch = epoch

                    self.train_epoch()

                    if self.lr_scheduler is not None:
                        if self.settings.scheduler_type != 'cosine':
                            self.lr_scheduler.step()
                        else:
                            self.lr_scheduler.step(epoch - 1)
                    # Only save the last 5 checkpoints
                    save_every_epoch = getattr(self.settings, 'save_every_epoch', False)
                    if epoch > (max_epochs - 5) or save_every_epoch or epoch % 100 == 0:
                        if self._checkpoint_dir:
                            if self.settings.local_rank in [-1, 0]:
                                self.save_checkpoint()
            except:
                print('training crashed at epoch {}'.format(epoch))
                if fail_safe:
                    self.epoch -= 1
                    load_latest = True
                    print('traceback for the error')
                    print(traceback.format_exc())
                    print('restarting training from last epoch ...')
                else:
                    raise

        if self.settings.local_rank in [-1, 0]:
            print('finished training')

    def train_epoch(self):
        raise NotImplementedError

    def save_checkpoint(self):
        """
        Saves a checkpoint of the network and other variables.
        """

        net = self.actor.net.module if multigpu.is_multi_gpu(self.actor.net) else self.actor.net

        actor_type = type(self.actor).__name__
        net_type = type(net).__name__
        state = {
            'epoch': self.epoch,  # <class 'int'>
            'actor_type': actor_type,  # <class 'str'>
            'net_type': net_type,  # <class 'str'>
            'net': net.state_dict(),  # <class 'collections.OrderedDict'>
            'net_info': getattr(net, 'info', None),  # <class 'NoneType'>
            'constructor': getattr(net, 'constructor', None),  # <class 'NoneType'>
            'optimizer': self.optimizer.state_dict(),  # <class 'dict'>
            'stats': self.stats,  # <class 'collections.OrderedDict'>
            'settings': self.settings  # <class 'lib.train.admin.settings.Settings'>
        }

        directory = '{}/{}'.format(self._checkpoint_dir, self.settings.project_path)
        if not os.path.exists(directory):
            # print("directory doesn't exist, creating ...")
            os.makedirs(directory)

        # First save as a tmp file
        tmp_file_path = '{}/{}_ep{:04d}.tmp'.format(directory, net_type, self.epoch)
        torch.save(state, tmp_file_path)

        file_path = '{}/{}_ep{:04d}.pth.tar'.format(directory, net_type, self.epoch)

        # Now rename to actual checkpoint. os.rename seems to be atomic if files are on same filesystem. Not 100% sure.
        os.rename(tmp_file_path, file_path)

    def load_checkpoint(self, checkpoint=None, fields=None, ignore_fields=None, load_constructor=False):
        """
        Loads a network checkpoint file.

        Can be called in three different ways:
            load_checkpoint():
                Loads the latest epoch from the workspace. Use this to continue training.
            load_checkpoint(epoch_num):
                Loads the network at the given epoch number (int).
            load_checkpoint(path_to_checkpoint):
                Loads the file from the given absolute path (str).
        """

        net = self.actor.net.module if multigpu.is_multi_gpu(self.actor.net) else self.actor.net

        actor_type = type(self.actor).__name__
        net_type = type(net).__name__

        if checkpoint is None:
            # Load most recent checkpoint
            checkpoint_list = sorted(glob.glob('{}/{}/{}_ep*.pth.tar'.format(self._checkpoint_dir,
                                                                             self.settings.project_path, net_type)))
            if checkpoint_list:
                checkpoint_path = checkpoint_list[-1]
                if self.settings.local_rank in [-1, 0]:
                    print('checkpoint file found')
            else:
                if self.settings.local_rank in [-1, 0]:
                    print('no matching checkpoint file found')
                return
        elif isinstance(checkpoint, int):
            # Checkpoint is the epoch number
            checkpoint_path = '{}/{}/{}_ep{:04d}.pth.tar'.format(self._checkpoint_dir, self.settings.project_path,
                                                                 net_type, checkpoint)
        elif isinstance(checkpoint, str):
            # Checkpoint is the path
            if os.path.isdir(checkpoint):
                checkpoint_list = sorted(glob.glob('{}/*_ep*.pth.tar'.format(checkpoint)))
                if checkpoint_list:
                    checkpoint_path = checkpoint_list[-1]
                else:
                    raise Exception('ERROR: no checkpoint found')
            else:
                checkpoint_path = os.path.expanduser(checkpoint)
        else:
            raise TypeError

        # Load network
        checkpoint_dict = torch.load(checkpoint_path)

        assert net_type == checkpoint_dict['net_type'], 'network is not of correct type'

        if fields is None:
            fields = checkpoint_dict.keys()
        if ignore_fields is None:
            ignore_fields = ['settings']

        # Never load the scheduler, it exists in older checkpoints
        ignore_fields.extend(['lr_scheduler', 'constructor', 'net_type', 'actor_type', 'net_info'])

        # Load all fields
        for key in fields:
            if key in ignore_fields:
                continue
            if key == 'net':
                net.load_state_dict(checkpoint_dict[key])
            elif key == 'optimizer':
                self.optimizer.load_state_dict(checkpoint_dict[key])
            else:
                setattr(self, key, checkpoint_dict[key])

        # Set the net info
        if load_constructor and 'constructor' in checkpoint_dict and checkpoint_dict['constructor'] is not None:
            net.constructor = checkpoint_dict['constructor']
        if 'net_info' in checkpoint_dict and checkpoint_dict['net_info'] is not None:
            net.info = checkpoint_dict['net_info']

        # Update the epoch in lr scheduler
        if 'epoch' in fields:
            self.lr_scheduler.last_epoch = self.epoch
            # Update the epoch in data_samplers
            for loader in self.loaders:
                if isinstance(loader.sampler, DistributedSampler):
                    loader.sampler.set_epoch(self.epoch)
        return True

    def load_state_dict(self, checkpoint=None):
        """
        Loads a network checkpoint file.

        Can be called in three different ways:
            load_checkpoint():
                Loads the latest epoch from the workspace. Use this to continue training.
            load_checkpoint(epoch_num):
                Loads the network at the given epoch number (int).
            load_checkpoint(path_to_checkpoint):
                Loads the file from the given absolute path (str).
        """

        net = self.actor.net.module if multigpu.is_multi_gpu(self.actor.net) else self.actor.net

        net_type = type(net).__name__

        if isinstance(checkpoint, str):
            # Checkpoint is the path
            if os.path.isdir(checkpoint):
                checkpoint_list = sorted(glob.glob('{}/*_ep*.pth.tar'.format(checkpoint)))
                if checkpoint_list:
                    checkpoint_path = checkpoint_list[-1]
                else:
                    raise Exception('ERROR: no checkpoint found')
            else:
                checkpoint_path = os.path.expanduser(checkpoint)
        else:
            raise TypeError

        # Load network
        print('loading pretrained model from', checkpoint_path)
        checkpoint_dict = torch.load(checkpoint_path, map_location='cpu')

        assert net_type == checkpoint_dict['net_type'], 'network is not of correct type'

        net.load_state_dict(checkpoint_dict['net'], strict=False)

        return True
