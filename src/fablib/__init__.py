#!/usr/bin/env python
# encoding: utf-8
"""
__init__.py

Created by Kurtiss Hare on 2010-08-15.
Copyright (c) 2010 Medium Entertainment, Inc. All rights reserved.
"""

from __future__ import with_statement

import collections
import functools
import os
import tempfile
import time

from contextlib import contextmanager
from functools import partial
from fabric.api import abort, cd, env, sudo, put, run, get, prompt
from fabric.state import _AttributeDict

        
def install(helper, scope, prefix = None):
    names = (n for n in dir(helper) if not n.startswith('_'))

    if prefix is None:
        prefix = ''
    else:
        prefix = "{0}_".format(prefix)

    for name in names:
        scope["{0}{1}".format(prefix, name)] = getattr(helper, name)
        

class Helper(object):
    def __init__(self):
        self._context = env

    @property
    def context(self):
        return self._context

    def _(self, s, **kwargs):
        if hasattr(s, 'format') and hasattr(s.format, '__call__'):
            if kwargs:
                params = dict((k,v) for (k,v) in self.context.items() + kwargs.items())    
            else:
                params = self.context

            return s.format(**params)

        return s

    def new(self, *items, **kwargs):
        kwargs.setdefault('base', _AttributeDict())
        return self._extend(items, kwargs)

    def extend(self, *items, **kwargs):
        kwargs['base'] = self.context
        self._context = self._extend(items, kwargs)

    def _extend(self, items, kwargs):
        try:
            base = kwargs['base']
        except KeyError:
            raise RuntimeError("'base' cannot be None.")

        for item_data in items:
            key, value, overwrite = (item_data + (False,))[:3]

            if getattr(base, key, None) is None or overwrite:
                if hasattr(value, '__call__'):
                    value = value()
                setattr(base, key, self._(value))

        return base

    def run(self, s, **kwargs):
        return run(self._(s, **kwargs))

    def sudo(self, s, **kwargs):
        return sudo(self._(s, **kwargs), pty = True)
    
    def abort(self, s, **kwargs):
        return abort(self._(s, **kwargs))

    def upload(self, local, remote, user, mode = 644, group = None):
        put(local, "/tmp/fabupload")
        self.sudo("""
            mv -f /tmp/fabupload {remote};
            chown {user}:{group} {remote};
            chmod {mode} {remote}
        """,
            user = self._(user),
            group = self._(group or user),
            mode = self._(str(mode)),
            remote = self._(remote)
        )
    
    def upload_rendered(self, local, remote, user, context = None, mode = 644, group = None):
        from jinja2 import Environment, FileSystemLoader
        je = Environment(loader = FileSystemLoader(self._(os.path.dirname(local))))
        template = je.get_template(self._(os.path.basename(local)))
        result = template.render(context or dict())
        with tempfile.NamedTemporaryFile() as f:
            f.write(result)
            if not result.endswith("\n"):
                f.write("\n")
            f.seek(0)
            self.upload(f.name, remote, user, mode = mode, group = group)
    
    def mkdirs(self, *paths):
        for path in paths:
            self.sudo("""
                mkdir -p {path};
                chown {user}:{group} {path}
                """,
                path = self._(path)
            )
    
    def get(self, *args, **kwargs):
        return get(*[self._(a) for a in args], **dict((self._(k), self._(v)) for (k,v) in kwargs))
    
    def prompt(self, text, key = None, default = '', validate = None):
        return prompt(self._(text), self._(key), default = self._(default), validate = validate)


class ProjectHelper(Helper):
    def __init__(self, github_path = None, project_path = None, packages = None):
        self.github_path = github_path
        self._project_path = project_path
        self.packages = set(["git-core"])

        if packages:
            self.packages.update(set(packages))
        
        super(ProjectHelper, self).__init__()

    def include_base_environment(self):
        github_account, application = self.github_path.split("/")

        self.extend(
            ('github_account',      github_account),
            ('application',         application),
            ('disable_known_hosts', True),
            ('keep_releases',       4),
            ('user',                "{application}-bot"),
            ('group',               "{user}"),
            ('key_filename',        partial(self.root_path, "home/{user}/.ssh/id_rsa")),
            ('path_prefix',         "/var/www"),
            ('path',                "{path_prefix}/{application}"),
            ('origin_uri',          "git@github.com:{0}.git".format(self.github_path)),
            ('shared_path',         "{path}/shared"),
            ('releases_path',       "{path}/releases"),
            ('current_path',        "{path}/current"),
            ('repository_path',     "{shared_path}/{application}")
        )

    def include_configure_environment(self):
        self.include_base_environment()

        self.extend(
            ('_user',           env.user),
            ('user',            "ubuntu"),
            ('key_filename',    self.etc_path("{application}/ec2/{application}-keypair.pem"))
        )
    
    def include_prepare_environment(self):
        self.include_base_environment()
        
    def include_deploy_environment(self, deploy_ref = "HEAD"):
        self.include_base_environment()

        self.extend(
            ('deploy_ref',      deploy_ref),
            ('release_id',      time.strftime("%Y%m%d%H%M%S")),
            ('release_path',    "{releases_path}/{release_id}")
        )

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

        self.addusers(self.context.user)

        put(
            self.root_path("etc/sudoers"),
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
            abort("sudoers file ({0}) did not pass validation".format(self.root_path("etc/sudoers")))

        sudo("mv -f /tmp/fabupload /etc/sudoers")
        
    def prepare(self):
        self.install_packages()
        self.sudo("rm -rf {repository_path}")

        self.mkdirs(
            "{path}", 
            "{shared_path}", 
            "{releases_path}", 
        )

        self.run("git clone {origin_uri} {repository_path}")
        self.update()
    
    def deploy(self):
        self.include_deploy_environment()

        self.deploy_update()
        self.deploy_alter()
        self.deploy_symlink()
        self.deploy_restart()
        self.deploy_cleanup()

    def deploy_update(self):
        self.update(self._("{deploy_ref}"))
        self.clone()

    def deploy_alter(self):
        pass
    
    def deploy_symlink(self):
        # symlink the new release
        self.run("""
            rm -f {current_path};
            ln -s {release_path} {current_path}
        """)
        
    def deploy_restart(self):
        pass

    def deploy_cleanup(self):
        # cleanup old releases
        with cd("{releases_path}".format(**self.context)):
            r = reversed(sudo("ls -dt */").splitlines())
            for dir in [d.strip('/') for d in r][:-self.context.keep_releases]:
                sudo("rm -rf {0}".format(dir))        
    
    def install_packages(self):
        self.sudo("apt-get install -y {packages}",
            packages = " ".join(self.packages)
        )

    def update(self, ref = "master"):
        self.run("""
            cd {repository_path};
            git fetch origin;
            git checkout {ref};
            git pull origin {ref};
            git checkout master;
            git submodule init;
            git submodule update;
        """,
            ref = ref
        )

    def clone(self):
        # clone the ref to our new release path
        self.run("""
            git clone {repository_path} {release_path};
            cd {release_path};
            git checkout {deploy_ref};
            git submodule init;
            git submodule update;
        """)

    def addusers(self, *users):
        for user in users:
            home_dir = self._("home/{user}", user = user)
            ssh_dir = home_dir + "/.ssh"

            self.sudo("""
                if [  "`cat /etc/passwd | grep {user}:`" = "" ]; then useradd -m -d /{home_dir} -s /bin/bash {user}; fi
                """,
                home_dir = home_dir,
                user = user
            )

            self.sudo("""
                mkdir -p /{ssh_dir};
                chown {user}:{group} /{ssh_dir}
                """,
                ssh_dir = ssh_dir,
                user = user
            )

            private_key_file = self._("{ssh_dir}/id_rsa", ssh_dir = ssh_dir)
            public_key_file = self._("{ssh_dir}/id_rsa.pub", ssh_dir = ssh_dir)
            ssh_config_file = self._("{ssh_dir}/config", ssh_dir = ssh_dir)

            FileInfo = collections.namedtuple('FileInfo', 'local remote mode')

            layout = [
                FileInfo(self.root_path(private_key_file), "/" + private_key_file, 600),
                FileInfo(self.root_path(public_key_file), "/" + public_key_file, 640),
                FileInfo(self.root_path(public_key_file), self._("/{ssh_dir}/authorized_keys", ssh_dir = ssh_dir), 640),
                FileInfo(self.root_path(ssh_config_file), "/" + ssh_config_file, 644)
            ]

            for info in layout:
                self.upload(info.local, info.remote, user, info.mode)

    def put(self, local, context=None, remote=None, user=None, mode=644, **kwargs):
        context = context or dict()
        updated_context = self.new(*(context.items() + kwargs.items()))

        remote = remote or os.path.join("{release_path}", local)

        self.upload_rendered(
            self.project_path(local),
            remote,
            user or self.context.user,
            updated_context,
            mode = mode,
        )

    def project_path(self, *paths):
        return self._(os.path.join(self._project_path, *paths))
    
    def etc_path(self, *paths):
        return self.project_path("etc", *paths)
    
    def src_path(self, *paths):
        return self.project_path("src", *paths)
    
    def root_path(self, *paths):
        return self.etc_path("{application}/root", *paths)
        

class PythonProjectHelper(ProjectHelper):
    def __init__(self, *args, **kwargs):
        self.pip_cmd = kwargs.get('pip_cmd', 'pip')

        packages = set(["python", "python-dev", "python-virtualenv", "python-pip"])
        packages.update(kwargs.get('packages', set()))
        kwargs['packages'] = packages

        super(PythonProjectHelper, self).__init__(*args, **kwargs)
    
    def include_base_environment(self):
        super(PythonProjectHelper, self).include_base_environment()

        self.extend(
            ('pip',             self.pip_cmd),
            ('log_path_prefix', "/var/log"),
            ('log_path',        "{log_path_prefix}/{application}"),
            ('pid_path_prefix', "/var/run"),
            ('pid_path',        "{pid_path_prefix}/{application}")
        )
    
    def include_deploy_environment(self, *args, **kwargs):
        super(PythonProjectHelper, self).include_deploy_environment(*args, **kwargs)
        self.extend(
            ('requirements_path', "{release_path}/etc/pip/requirements.txt")
        )
    
    def prepare(self):
        super(PythonProjectHelper, self).prepare()

        self.mkdirs(
            "{log_path}",
            "{pid_path}"
        )

    def pip(self, s, **kwargs):
        self.sudo("{{pip}} {0}".format(s), **kwargs)

    def install_packages(self):
        super(PythonProjectHelper, self).install_packages()
        self.pip("install virtualenvwrapper")

    def clone(self):
        super(PythonProjectHelper, self).clone()

        self.run_in_virtualenv("""
            easy_install pip;

            if [ -e "{release_path}/bin/install" ];
            then
                source {release_path}/bin/install
            else
                {pip} install -i http://d.pypi.python.org/simple -r {requirements_path};    
                add2virtualenv {release_path}/src
            fi;
        """)

    def run_in_virtualenv(self, command):
        self.run("""
            pushd . 1>/dev/null;
            export WORKON_HOME={releases_path};
            source virtualenvwrapper.sh;
            mkvirtualenv --no-site-packages {release_id};
            workon {release_id};
            cdvirtualenv
            %(command)s
            deactivate;
            popd
        """ % dict(command=command))
