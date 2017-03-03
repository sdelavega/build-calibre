#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import (unicode_literals, division, absolute_import,
                        print_function)

import subprocess
import os
import plistlib
from glob import glob


from pkgs.constants import CODESIGN_KEYCHAIN
from pkgs.utils import current_dir


def codesign(items):
    if isinstance(items, basestring):
        items = [items]
    # If you get errors while codesigning that look like "A timestamp was
    # expected but not found" it means that codesign  failed to contact Apple's time
    # servers, probably due to network congestion, so add --timestamp=none to
    # this command line. That means the signature will fail once your code
    # signing key expires and key revocation wont work, but...
    if items:
        subprocess.check_call(['codesign', '-s', 'Kovid Goyal', '--keychain', CODESIGN_KEYCHAIN] + list(items))


def files_in(folder):
    for record in os.walk(folder):
        for f in record[-1]:
            yield os.path.join(record[0], f)


def expand_dirs(items):
    items = set(items)
    dirs = set(x for x in items if os.path.isdir(x))
    items.difference_update(dirs)
    for x in dirs:
        items.update(set(files_in(x)))
    return items


def get_executable(info_path):
    return plistlib.readPlist(info_path)['CFBundleExecutable']


def sign_app(appdir):
    appdir = os.path.abspath(appdir)
    subprocess.check_call(['security', 'unlock-keychain', '-p', 'keychains are stupid', CODESIGN_KEYCHAIN])
    with current_dir(os.path.join(appdir, 'Contents')):
        executables = {get_executable('Info.plist')}

        # Sign everything in MacOS except the main executable
        # which will be signed automatically by codesign when
        # signing the app bundle
        with current_dir('MacOS'):
            items = set(os.listdir('.')) - executables
            codesign(expand_dirs(items))

        # Sign everything in Frameworks
        with current_dir('Frameworks'):
            fw = set(glob('*.framework'))
            codesign(fw)
            items = set(os.listdir('.')) - fw
            codesign(expand_dirs(items))

    # Now sign the main app
    codesign(appdir)
    # Verify the signature
    subprocess.check_call(['codesign', '--deep', '--verify', '-v', appdir])
    subprocess.check_call('spctl --verbose=4 --assess --type execute'.split() + [appdir])

    return 0
