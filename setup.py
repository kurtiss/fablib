#!/usr/bin/env python
# encoding: utf-8
"""
setup.py

Created by Kurtiss Hare on 2010-08-15.
Copyright (c) 2010 Medium Entertainment, Inc. All rights reserved.
"""

from setuptools import setup, find_packages
import os

srcpath = "src"
srcroot = os.path.join(os.path.dirname(__file__), srcpath)
packages = os.listdir(srcroot)

for package in packages:
    os.symlink(os.path.join(srcroot, package), package)

execfile(os.path.join('fablib', 'version.py'))

setup(
    name = 'fablib',
    version = VERSION,
    description = 'fablib makes available a few primitives of use to fabric control layers.',
    author = 'Kurtiss Hare',
    author_email = 'kurtiss@gmail.com',
    url = 'http://www.github.com/kurtiss/fablib',
    packages = find_packages(),
    scripts = [],
    classifiers = [
        'Intended Audience :: Developers',
        'Operating System :: OS Independent',
        'Programming Language :: Python',
        'Topic :: Software Development :: Libraries :: Python Modules'
    ],
    zip_safe = False
)

for package in packages:
    os.remove(package)