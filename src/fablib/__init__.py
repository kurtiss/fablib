#!/usr/bin/env python
# encoding: utf-8
"""
__init__.py

Created by Kurtiss Hare on 2010-08-15.
Copyright (c) 2010 Medium Entertainment, Inc. All rights reserved.
"""

from __future__ import with_statement
from fabric.api import abort, cd, env, sudo, put, run
import collections, os, time


def _(s, **kwargs):
    if kwargs:
        params = dict((k,v) for (k,v) in env.items() + kwargs.items())
    else:
        params = env

    return s.format(**params)


class BaseHelper(object):
    def __init__(self, github_path = None, file = None, packages = None, hosts = None):
        self.github_path = github_path
        self.file = file

        self.packages = set(["git-core"])
        if packages:
            self.packages.update(set(packages))

        self.hosts = hosts or []

    def base(self):
        env.github_account, env.application = self.github_path.split("/")

        env.disable_known_hosts = True
        env.hosts = self.hosts
        env.keep_releases = 4

        env.path = _("/var/www/{application}")
        env.origin_uri = _("git@github.com:{github_account}/{application}.git")

        env.shared_path = _("{path}/shared")
        env.releases_path = _("{path}/releases")
        env.current_path = _("{path}/current")
        env.repository_path = _("{shared_path}/{application}")

    def fresh(self):
        self.base()
        env.user = "ubuntu"
        env.key_filename = [self.localpath("{application}/ec2/{application}-keypair.pem")]

    def stage(self):
        self.base()
        env.user = _("{application}-bot")
        env.key_filename = [self.rootpath("home/{user}/.ssh/id_rsa")]

    def configure(self):
        sudo(
            """
            DEBIAN_FRONTEND=noninteractive DEBIAN_PRIORITY=critical
            aptitude dist-upgrade -y &&
            apt-get update &&
            apt-get upgrade -y
            """,
            pty = True
        )

        self.addusers(_("{application}-bot"))

        put(
            self.rootpath("etc/sudoers"),
            "/tmp/fabupload"
        )

        sudoers_output = sudo("""
            chown root:root /tmp/fabupload;
            chmod 440 /tmp/fabupload;
            visudo -c -f /tmp/fabupload;
            echo $?
            """
        )

        if sudoers_output.splitlines()[-1] != "0":
            sudo("rm -f /tmp/fabupload")
            abort("sudoers file ({0}) did not pass validation".format(self.rootpath("etc/sudoers")))

        sudo("mv -f /tmp/fabupload /etc/sudoers")

    def run(self, s, **kwargs):
        return run(_(s, **kwargs))

    def sudo(self, s, **kwargs):
        return sudo(_(s, **kwargs), pty = True)

    def install_packages(self):
        self.sudo("apt-get install -y {packages}",
            packages = " ".join(self.packages)
        )

    def setup(self):
        self.install_packages()
        self.sudo("rm -rf {repository_path}")

        self.mkdirs(
            _("{path}"),
            _("{shared_path}"),
            _("{releases_path}"),
            _("{repository_path}")
        )

        self.run("git clone {origin_uri} {repository_path}")
        self.update()

    def update(self):
        self.run("""
            cd {repository_path};
            git pull origin master;
            git submodule update --init;
        """)

    def deploy(self, deploy_ref = "HEAD"):
        env.deploy_ref = deploy_ref
        env.release_id = time.strftime("%Y%m%d%H%M%S")
        env.release_path = _("{releases_path}/{release_id}")

        self.update()
        self.clone()

        # symlink the new release
        run("ln -f -s {release_path} {current_path}".format(**env))

        # cleanup old releases
        with cd("{releases_path}".format(**env)):
            r = reversed(sudo("ls -dt */").splitlines())
            for dir in [d.strip('/') for d in r][:-env.keep_releases]:
                sudo("rm -rf {0}".format(dir))

    def clone(self):
        # clone the ref to our new release path
        self.run("""
            git clone {repository_path} {release_path};
            cd {release_path};
            git checkout {deploy_ref}
        """)


    def mkdirs(self, *paths):
        for path in paths:
            self.sudo("""
                mkdir -p {path};
                chown {user}:{user} {path}
                """,
                path = path
            )

    def addusers(self, *users):
        for user in users:
            home_dir = _("home/{user}", user = user)
            ssh_dir = home_dir + "/.ssh"

            self.sudo("""
                if [  "`cat /etc/passwd | grep {user}:`" = "" ]; then useradd -m -d /{home_dir} -s /bin/bash {user}; fi
                """,
                home_dir = home_dir,
                user = user
            )

            self.sudo("""
                mkdir -p /{ssh_dir};
                chown {user}:{user} /{ssh_dir}
                """,
                ssh_dir = ssh_dir,
                user = user
            )

            private_key_file = _("{ssh_dir}/id_rsa", ssh_dir = ssh_dir)
            public_key_file = _("{ssh_dir}/id_rsa.pub", ssh_dir = ssh_dir)
            ssh_config_file = _("{ssh_dir}/config", ssh_dir = ssh_dir)

            FileInfo = collections.namedtuple('FileInfo', 'local remote mode')

            layout = [
                FileInfo(self.rootpath(private_key_file), "/" + private_key_file, 600),
                FileInfo(self.rootpath(public_key_file), "/" + public_key_file, 640),
                FileInfo(self.rootpath(public_key_file), _("/{ssh_dir}/authorized_keys", ssh_dir = ssh_dir), 640),
                FileInfo(self.rootpath(ssh_config_file), "/" + ssh_config_file, 644)
            ]

            for info in layout:
                put(info.local, "/tmp/fabupload")
                self.sudo("""
                    mv /tmp/fabupload {info.remote};
                    chown {user}:{user} {info.remote};
                    chmod {info.mode} {info.remote};
                    """,
                    user = user,
                    info = info
                )

    def localpath(self, path = ""):
        return os.path.join(os.path.dirname(os.path.dirname(self.file)), _(path))

    def rootpath(self, path = ""):
        return self.localpath(os.path.join(_("{application}/root"), _(path)))


class PythonProjectHelper(BaseHelper):
    def __init__(self, *args, **kwargs):
        self.pip_cmd = kwargs.get('pip_cmd', 'pip')

        packages = set(["python", "python-dev", "python-virtualenv", "python-pip"])
        packages.update(kwargs.get('packages', set()))
        kwargs['packages'] = packages

        super(PythonProjectHelper, self).__init__(*args, **kwargs)

    def base(self):
        super(PythonProjectHelper, self).base()
        env.pip = self.pip_cmd

    def pip(self, s, **kwargs):
        self.sudo("{{pip}} {0}".format(s), **kwargs)

    def install_packages(self):
        super(PythonProjectHelper, self).install_packages()
        self.pip("install virtualenvwrapper")

    def setup(self):
        super(PythonProjectHelper, self).setup()
        self.init_virtualenvwrapper()

    def clone(self):
        super(PythonProjectHelper, self).clone()
        env.requirements_path = _("{release_path}/etc/pip/requirements.txt")

        self.init_virtualenvwrapper("""
            mkvirtualenv --no-site-packages {release_id};
            workon {release_id};
            easy_install pip;
            {pip} install -r {requirements_path};
            add2virtualenv {release_path}/src
        """)

    def init_virtualenvwrapper(self, code = ""):
        self.run("""
            export WORKON_HOME={releases_path};
            source virtualenvwrapper.sh;
            """ + code
        )


class ShrapnelProjectHelper(PythonProjectHelper):
    def __init__(self, *args, **kwargs):
        packages = kwargs.get('packages', set())
        packages.update(['libcurl4-openssl-dev'])
        kwargs['packages'] = packages
        super(ShrapnelProjectHelper, self).__init__(*args, **kwargs)