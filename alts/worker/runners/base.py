import logging
import os
import shutil
import tempfile
import time
from functools import wraps
from pathlib import Path
from typing import List, Union

import boto3
from boto3.exceptions import S3UploadFailedError
from mako.lookup import TemplateLookup
from plumbum import local

from alts.shared.exceptions import (
    InstallPackageError, ProvisionError, PublishArtifactsError,
    StartEnvironmentError, StopEnvironmentError, TerraformInitializationError,
    WorkDirPreparationError,
)
from alts.shared.types import ImmutableDict
from alts.worker import CONFIG, RESOURCES_DIR


__all__ = ['BaseRunner', 'GenericVMRunner', 'command_decorator']


def command_decorator(exception_class, artifacts_key, error_message):
    def method_wrapper(fn):
        @wraps(fn)
        def inner_wrapper(*args, **kwargs):
            self = args[0]
            args = args[1:]
            if not self._work_dir or not os.path.exists(self._work_dir):
                return
            exit_code, stdout, stderr = fn(self, *args, **kwargs)
            self._artifacts[artifacts_key] = {
                'exit_code': exit_code,
                'stderr': stderr,
                'stdout': stdout
            }
            if exit_code != 0:
                self._logger.error(f'{error_message}, exit code: {exit_code},'
                                   f' error:\n{stderr}')
                raise exception_class(error_message)
            else:
                self._logger.info('Operation completed successfully')
            # Return results of command execution
            return exit_code, stdout, stderr
        return inner_wrapper
    return method_wrapper


class BaseRunner(object):
    """
    This class describes a basic interface of test runner on some instance
    like Docker container, virtual machine, etc.

    """
    DEBIAN_FLAVORS = ('debian', 'ubuntu', 'raspbian')
    RHEL_FLAVORS = ('fedora', 'centos', 'almalinux', 'cloudlinux')
    TYPE = 'base'
    ARCHITECTURES_MAPPING = ImmutableDict(
        aarch64=['arm64', 'aarch64'],
        x86_64=['x86_64', 'amd64', 'i686'],
    )
    COST = 0
    TF_VARIABLES_FILE = None
    TF_MAIN_FILE = None
    TF_VERSIONS_FILE = 'versions.tf'
    ANSIBLE_PLAYBOOK = 'playbook.yml'
    ANSIBLE_CONFIG = 'ansible.cfg'
    ANSIBLE_INVENTORY_FILE = 'hosts'
    TEMPFILE_PREFIX = 'base_test_runner_'

    def __init__(self, task_id: str, dist_name: str,
                 dist_version: Union[str, int],
                 repositories: List[dict] = None, dist_arch: str = 'x86_64'):
        # Environment ID and working directory preparation
        self._task_id = task_id
        self._env_name = f'{self.TYPE}_{task_id}'
        self._logger = logging.getLogger(__file__)
        self._work_dir = None
        self._artifacts_dir = None
        self._class_resources_dir = os.path.join(RESOURCES_DIR, self.TYPE)
        self._template_lookup = TemplateLookup(
            directories=[RESOURCES_DIR, self._class_resources_dir])

        # Basic commands and tools setup
        self._ansible_connection_type = 'ssh'

        # Package-specific variables that define needed container/VM
        self._dist_name = dist_name.lower()
        self._dist_version = str(dist_version).lower()
        self._dist_arch = dist_arch.lower()

        # Package installation and test stuff
        self._repositories = repositories or []

        self._artifacts = {}

    @property
    def artifacts(self):
        return self._artifacts

    @property
    def pkg_manager(self):
        if (self._dist_name == 'fedora' or self._dist_name in self.RHEL_FLAVORS
                and '8' in self._dist_version):
            return 'dnf'
        elif self._dist_name in self.RHEL_FLAVORS:
            return 'yum'
        elif self._dist_name in self.DEBIAN_FLAVORS:
            return 'apt-get'
        else:
            raise ValueError(f'Unknown distribution: {self._dist_name}')

    @property
    def ansible_connection_type(self):
        return self._ansible_connection_type

    @property
    def dist_arch(self):
        return self._dist_arch

    @property
    def dist_name(self):
        return self._dist_name

    @property
    def dist_version(self):
        return self._dist_version

    @property
    def repositories(self):
        return self._repositories

    @property
    def env_name(self):
        return self._env_name

    # TODO: Think of better implementation
    def _create_work_dir(self):
        if not self._work_dir or not os.path.exists(self._work_dir):
            self._work_dir = Path(tempfile.mkdtemp(prefix=self.TEMPFILE_PREFIX))
        return self._work_dir

    # TODO: Think of better implementation
    def _create_artifacts_dir(self):
        if not self._work_dir:
            self._work_dir = self._create_work_dir()
        path = self._work_dir / 'artifacts'
        if not os.path.exists(path):
            os.mkdir(path)
        return path

    def __del__(self):
        self.stop_env()
        self.erase_work_dir()

    def _render_template(self, template_name, result_file_path, **kwargs):
        template = self._template_lookup.get_template(template_name)
        with open(result_file_path, 'wt') as f:
            content = template.render(**kwargs)
            f.write(content)

    def _create_ansible_inventory_file(self, vm_ip: str = None):
        inventory_file_path = os.path.join(self._work_dir,
                                           self.ANSIBLE_INVENTORY_FILE)
        self._render_template(
            f'{self.ANSIBLE_INVENTORY_FILE}.tmpl', inventory_file_path,
            env_name=self.env_name, vm_ip=vm_ip
        )

    def _render_tf_main_file(self):
        """
        Renders main Terraform file for the instance managing

        Returns:

        """
        raise NotImplementedError

    def _render_tf_variables_file(self):
        """
        Renders Terraform variables file

        Returns:

        """
        raise NotImplementedError

    # First step
    def prepare_work_dir_files(self, create_ansible_inventory=False):
        # In case if you've removed worker folder, recreate one
        if not self._work_dir or not os.path.exists(self._work_dir):
            self._work_dir = self._create_work_dir()
            self._artifacts_dir = self._create_artifacts_dir()
        try:
            # Write resources that are not templated into working directory
            for ansible_file in (self.ANSIBLE_CONFIG, self.ANSIBLE_PLAYBOOK):
                shutil.copy(os.path.join(RESOURCES_DIR, ansible_file),
                            os.path.join(self._work_dir, ansible_file))
            shutil.copy(
                os.path.join(self._class_resources_dir, self.TF_VERSIONS_FILE),
                os.path.join(self._work_dir, self.TF_VERSIONS_FILE)
            )

            if create_ansible_inventory:
                self._create_ansible_inventory_file()
            self._render_tf_main_file()
            self._render_tf_variables_file()
        except Exception as e:
            raise WorkDirPreparationError('Cannot create working directory and'
                                          ' needed files') from e

    # After: prepare_work_dir_files
    @command_decorator(TerraformInitializationError, 'initialize_terraform',
                       'Cannot initialize terraform')
    def initialize_terraform(self):
        self._logger.info(f'Initializing Terraform environment '
                          f'for {self.env_name}...')
        self._logger.debug('Running "terraform init" command')
        return local['terraform'].run('init', retcode=None, cwd=self._work_dir)

    # After: initialize_terraform
    @command_decorator(StartEnvironmentError, 'start_environment',
                       'Cannot start environment')
    def start_env(self):
        self._logger.info(f'Starting the environment {self.env_name}...')
        self._logger.debug('Running "terraform apply --auto-approve" command')
        cmd_args = ['apply', '--auto-approve']
        if self.TF_VARIABLES_FILE:
            cmd_args.extend(['--var-file', self.TF_VARIABLES_FILE])
        return local['terraform'].run(args=cmd_args, retcode=None,
                                      cwd=self._work_dir)

    # After: start_env
    @command_decorator(ProvisionError, 'initial_provision',
                       'Cannot run initial provision')
    def initial_provision(self, verbose=False):
        cmd_args = ['-c', self.ansible_connection_type, '-i',
                    self.ANSIBLE_INVENTORY_FILE, self.ANSIBLE_PLAYBOOK,
                    '-e', f'repositories={self._repositories}',
                    '-t', 'initial_provision']
        if verbose:
            cmd_args.append('-vvvv')
        cmd_args_str = ' '.join(cmd_args)
        self._logger.info(f'Provisioning the environment {self.env_name}...')
        self._logger.debug(
            f'Running "ansible-playbook {cmd_args_str}" command')
        return local['ansible-playbook'].run(
            args=cmd_args, retcode=None, cwd=self._work_dir)

    @command_decorator(InstallPackageError, 'install_package',
                       'Cannot install package')
    def install_package(self, package_name: str, package_version: str = None):
        if package_version:
            if self.pkg_manager == 'yum':
                full_pkg_name = f'{package_name}-{package_version}'
            else:
                full_pkg_name = f'{package_name}={package_version}'
        else:
            full_pkg_name = package_name

        self._logger.info(f'Installing {full_pkg_name} on {self.env_name}...')
        cmd_args = ['-c', self.ansible_connection_type, '-i',
                    self.ANSIBLE_INVENTORY_FILE, self.ANSIBLE_PLAYBOOK,
                    '-e', f'pkg_name={full_pkg_name}',
                    '-t', 'install_package']
        cmd_args_str = ' '.join(cmd_args)
        self._logger.debug(
            f'Running "ansible-playbook {cmd_args_str}" command')
        return local['ansible-playbook'].run(
            args=cmd_args, retcode=None, cwd=self._work_dir)

    def publish_artifacts_to_storage(self):
        # Should upload artifacts from artifacts directory to preferred
        # artifacts storage (S3, Minio, etc.)
        for artifact_key, content in self.artifacts.items():
            log_file_path = os.path.join(self._artifacts_dir,
                                         f'{artifact_key}.log')
            with open(log_file_path, 'w+t') as f:
                f.write(f'Exit code: {content["exit_code"]}\n')
                f.write(content['stdout'])
            if content['stderr']:
                error_log_path = os.path.join(self._artifacts_dir,
                                              f'{artifact_key}_error.log')
                with open(error_log_path, 'w+t') as f:
                    f.write(content['stderr'])

        client = boto3.client(
            's3', region_name=CONFIG.s3_region,
            aws_access_key_id=CONFIG.s3_access_key_id,
            aws_secret_access_key=CONFIG.s3_secret_access_key
        )
        error_when_uploading = False
        for artifact in os.listdir(self._artifacts_dir):
            artifact_path = os.path.join(self._artifacts_dir, artifact)
            object_name = os.path.join(CONFIG.artifacts_root_directory,
                                       self._task_id, artifact)
            try:
                self._logger.info(f'Uploading artifact {artifact_path} to S3')
                client.upload_file(artifact_path, CONFIG.s3_bucket, object_name)
            except (S3UploadFailedError, ValueError) as e:
                self._logger.error(f'Cannot upload artifact {artifact_path}'
                                   f' to S3: {e}')
                error_when_uploading = True
        if error_when_uploading:
            raise PublishArtifactsError('One or more artifacts were not'
                                        ' uploaded')

    # After: install_package and run_tests
    @command_decorator(StopEnvironmentError, 'stop_environment',
                       'Cannot destroy environment')
    def stop_env(self):
        if os.path.exists(self._work_dir):
            self._logger.info(f'Destroying the environment {self.env_name}...')
            self._logger.debug(
                'Running "terraform destroy --auto-approve" command')
            cmd_args = ['destroy', '--auto-approve']
            if self.TF_VARIABLES_FILE:
                cmd_args.extend(['--var-file', self.TF_VARIABLES_FILE])
            return local['terraform'].run(args=cmd_args, retcode=None,
                                          cwd=self._work_dir)

    def erase_work_dir(self):
        if self._work_dir and os.path.exists(self._work_dir):
            self._logger.info('Erasing working directory...')
            try:
                shutil.rmtree(self._work_dir)
            except Exception as e:
                self._logger.error(f'Error while erasing working directory:'
                                   f' {e}')
            else:
                self._logger.info('Working directory was successfully removed')

    def setup(self):
        self.prepare_work_dir_files()
        self.initialize_terraform()
        self.start_env()
        self.initial_provision()

    def teardown(self, publish_artifacts: bool = True):
        self.stop_env()
        if publish_artifacts:
            self.publish_artifacts_to_storage()
        self.erase_work_dir()


class GenericVMRunner(BaseRunner):

    def __init__(self, task_id: str, dist_name: str,
                 dist_version: Union[str, int],
                 repositories: List[dict] = None, dist_arch: str = 'x86_64'):
        super().__init__(task_id, dist_name, dist_version,
                         repositories=repositories, dist_arch=dist_arch)
        ssh_key_path = os.path.abspath(
            os.path.expanduser(CONFIG.ssh_public_key_path))
        if not os.path.exists(ssh_key_path):
            self._logger.error('SSH key is missing')
        else:
            with open(ssh_key_path, 'rt') as f:
                self._ssh_public_key = f.read().strip()

    @property
    def ssh_public_key(self):
        return self._ssh_public_key

    def _wait_for_ssh(self, retries=60):
        ansible = local['ansible']
        cmd_args = ('-i', self.ANSIBLE_INVENTORY_FILE, '-m', 'ping', 'all')
        stdout = None
        stderr = None
        while retries > 0:
            exit_code, stdout, stderr = ansible.run(
                args=cmd_args, retcode=None, cwd=self._work_dir)
            if exit_code == 0:
                return True
            else:
                retries -= 1
                time.sleep(10)
        self._logger.error(f'Unable to connect to VM. '
                           f'Stdout: {stdout}\nStderr: {stderr}')
        return False

    def start_env(self):
        super().start_env()
        # VM gets its IP address only after deploy.
        # To extract it, the `vm_ip` output should be defined
        # in Terraform main file.
        exit_code, stdout, stderr = local['terraform'].run(
            args=('output', '-raw', 'vm_ip'), retcode=None, cwd=self._work_dir)
        if exit_code != 0:
            error_message = f'Cannot get VM IP: {stderr}'
            self._logger.error(error_message)
            raise StartEnvironmentError(error_message)
        self._create_ansible_inventory_file(vm_ip=stdout)
        self._logger.info('Waiting for SSH port to be available')
        is_online = self._wait_for_ssh()
        if not is_online:
            error_message = f'Machine {self.env_name} is started, but ' \
                            f'SSH connection is not working'
            self._logger.error(error_message)
            raise StartEnvironmentError(error_message)
        self._logger.info(f'Machine is available for SSH connection')