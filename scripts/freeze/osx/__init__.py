#!/usr/bin/env python2
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import errno
import operator
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from pkgs.constants import KITTY_DIR, PREFIX, SW, get_py_ver
from pkgs.utils import current_dir, timeit, walk, run_shell

from .. import kitty_constants, py_compile
from .sign import sign_app

abspath, join, basename, dirname = os.path.abspath, os.path.join, os.path.basename, os.path.dirname

LICENSE = open('LICENSE', 'rb').read()
APPNAME, VERSION = kitty_constants['appname'], kitty_constants['version']


def flipwritable(fn, mode=None):
    """
    Flip the writability of a file and return the old mode. Returns None
    if the file is already writable.
    """
    if os.access(fn, os.W_OK):
        return None
    old_mode = os.stat(fn).st_mode
    os.chmod(fn, stat.S_IWRITE | old_mode)
    return old_mode


STRIPCMD = ['/usr/bin/strip', '-x', '-S', '-']


def strip_files(files, argv_max=(256 * 1024)):
    """
    Strip a list of files
    """
    tostrip = [(fn, flipwritable(fn)) for fn in files if os.path.exists(fn)]
    while tostrip:
        cmd = list(STRIPCMD)
        flips = []
        pathlen = reduce(operator.add, [len(s) + 1 for s in cmd])
        while pathlen < argv_max:
            if not tostrip:
                break
            added, flip = tostrip.pop()
            pathlen += len(added) + 1
            cmd.append(added)
            flips.append((added, flip))
        else:
            cmd.pop()
            tostrip.append(flips.pop())
        os.spawnv(os.P_WAIT, cmd[0], cmd)
        for args in flips:
            flipwritable(*args)


def flush(func):
    def ff(*args, **kwargs):
        sys.stdout.flush()
        sys.stderr.flush()
        ret = func(*args, **kwargs)
        sys.stdout.flush()
        sys.stderr.flush()
        return ret

    return ff


class Freeze(object):

    FID = '@executable_path/../Frameworks'

    def __init__(self, build_dir, dont_strip=False, sign_installers=False):
        self.build_dir = build_dir
        self.sign_installers = sign_installers
        self.dont_strip = dont_strip
        self.contents_dir = join(self.build_dir, 'Contents')
        self.resources_dir = join(self.contents_dir, 'Resources')
        self.frameworks_dir = join(self.contents_dir, 'Frameworks')
        self.to_strip = []
        self.warnings = []
        self.py_ver = get_py_ver()

        self.run()

    def run_shell(self):
        cwd = os.getcwd()
        os.chdir(self.contents_dir)
        run_shell()
        os.chdir(cwd)

    def run(self):
        ret = 0
        self.create_skeleton()
        self.add_python_framework()
        self.add_stdlib()
        self.add_misc_libraries()
        self.copy_site()
        self.compile_py_modules()
        if not self.dont_strip:
            self.strip_files()
        # self.run_shell()

        ret = self.makedmg(self.build_dir, APPNAME + '-' + VERSION)

        return ret

    @flush
    def strip_files(self):
        print('\nStripping files...')
        strip_files(self.to_strip)

    @flush
    def set_id(self, path_to_lib, new_id):
        old_mode = flipwritable(path_to_lib)
        subprocess.check_call(
            ['install_name_tool', '-id', new_id, path_to_lib])
        if old_mode is not None:
            flipwritable(path_to_lib, old_mode)

    @flush
    def get_dependencies(self, path_to_lib):
        install_name = subprocess.check_output(
            ['otool', '-D', path_to_lib]).splitlines()[-1].strip()
        raw = subprocess.check_output(['otool', '-L', path_to_lib])
        for line in raw.splitlines():
            if 'compatibility' not in line or line.strip().endswith(':'):
                continue
            idx = line.find('(')
            path = line[:idx].strip()
            yield path, path == install_name

    @flush
    def get_local_dependencies(self, path_to_lib):
        for x, is_id in self.get_dependencies(path_to_lib):
            for y in (PREFIX + '/lib/', PREFIX + '/python/Python.framework/'):
                if x.startswith(y):
                    if y == PREFIX + '/python/Python.framework/':
                        y = PREFIX + '/python/'
                    yield x, x[len(y):], is_id
                    break

    @flush
    def change_dep(self, old_dep, new_dep, is_id, path_to_lib):
        cmd = ['-id', new_dep] if is_id else ['-change', old_dep, new_dep]
        subprocess.check_call(['install_name_tool'] + cmd + [path_to_lib])

    @flush
    def fix_dependencies_in_lib(self, path_to_lib):
        self.to_strip.append(path_to_lib)
        old_mode = flipwritable(path_to_lib)
        for dep, bname, is_id in self.get_local_dependencies(path_to_lib):
            ndep = self.FID + '/' + bname
            self.change_dep(dep, ndep, is_id, path_to_lib)
        ldeps = list(self.get_local_dependencies(path_to_lib))
        if ldeps:
            print('\nFailed to fix dependencies in', path_to_lib)
            print('Remaining local dependencies:', ldeps)
            raise SystemExit(1)
        if old_mode is not None:
            flipwritable(path_to_lib, old_mode)

    @flush
    def add_python_framework(self):
        print('\nAdding Python framework')
        src = join(PREFIX + '/python', 'Python.framework')
        x = join(self.frameworks_dir, 'Python.framework')
        curr = os.path.realpath(join(src, 'Versions', 'Current'))
        currd = join(x, 'Versions', basename(curr))
        rd = join(currd, 'Resources')
        os.makedirs(rd)
        shutil.copy2(join(curr, 'Resources', 'Info.plist'), rd)
        shutil.copy2(join(curr, 'Python'), currd)
        self.set_id(
            join(currd, 'Python'),
            self.FID + '/Python.framework/Versions/%s/Python' % basename(curr))
        self.fix_dependencies_in_lib(join(self.contents_dir, 'MacOS', 'kitty'))
        # The following is needed for codesign
        with current_dir(x):
            os.symlink(basename(curr), 'Versions/Current')
            for y in ('Python', 'Resources'):
                os.symlink('Versions/Current/%s' % y, y)

    @flush
    def create_skeleton(self):
        x = join(KITTY_DIR, 'logo', APPNAME + '.iconset')
        if not os.path.exists(x):
            raise SystemExit('Failed to find icns format icons')
        subprocess.check_call([
            'iconutil', '-c', 'icns', x, '-o',
            join(self.resources_dir, basename(x).partition('.')[0] + '.icns')
        ])
        self.create_plist()

    @flush
    def create_plist(self):
        pl = dict(
            CFBundleDevelopmentRegion='English',
            CFBundleDisplayName=APPNAME,
            CFBundleName=APPNAME,
            CFBundleIdentifier='net.kovidgoyal.' + APPNAME,
            CFBundleVersion=VERSION,
            CFBundleShortVersionString=VERSION,
            CFBundlePackageType='APPL',
            CFBundleSignature='????',
            CFBundleExecutable=APPNAME,
            LSMinimumSystemVersion='10.9.5',
            LSRequiresNativeExecution=True,
            NSAppleScriptEnabled=False,
            NSHumanReadableCopyright=time.strftime(
                'Copyright %Y, Kovid Goyal'),
            CFBundleGetInfoString='kitty, an OpenGL based terminal emulator https://github.com/kovidgoyal/kitty',
            CFBundleIconFile=APPNAME + '.icns',
            NSHighResolutionCapable=True,
            LSApplicationCategoryType='public.app-category.utilities',
            LSEnvironment={'KITTY_LAUNCHED_BY_LAUNCH_SERVICES': '1'},
        )
        plistlib.writePlist(pl, join(self.contents_dir, 'Info.plist'))

    @flush
    def install_dylib(self, path, set_id=True):
        shutil.copy2(path, self.frameworks_dir)
        if set_id:
            self.set_id(
                join(self.frameworks_dir, basename(path)),
                self.FID + '/' + basename(path))
        self.fix_dependencies_in_lib(join(self.frameworks_dir, basename(path)))

    @flush
    def add_misc_libraries(self):
        for x in (
                'sqlite3.0',
                'z.1',
                'glfw.3',
                'crypto.1.0.0',
                'ssl.1.0.0',
        ):
            print('\nAdding', x)
            x = 'lib%s.dylib' % x
            src = join(PREFIX, 'lib', x)
            shutil.copy2(src, self.frameworks_dir)
            dest = join(self.frameworks_dir, x)
            self.set_id(dest, self.FID + '/' + x)
            self.fix_dependencies_in_lib(dest)
        base = join(self.frameworks_dir, 'kitty')
        for lib in walk(base):
            if lib.endswith('.so'):
                self.set_id(lib, self.FID + '/' + os.path.relpath(lib, self.frameworks_dir))
                self.fix_dependencies_in_lib(lib)

    @flush
    def add_package_dir(self, x, dest=None):
        def ignore(root, files):
            ans = []
            for y in files:
                ext = os.path.splitext(y)[1]
                if ext not in ('', '.py', '.so') or \
                        (not ext and not os.path.isdir(join(root, y))):
                    ans.append(y)

            return ans

        if dest is None:
            dest = self.site_packages
        dest = join(dest, basename(x))
        shutil.copytree(x, dest, symlinks=True, ignore=ignore)
        self.postprocess_package(x, dest)
        for f in walk(dest):
            if f.endswith('.so'):
                self.fix_dependencies_in_lib(f)

    @flush
    def postprocess_package(self, src_path, dest_path):
        pass

    @flush
    def add_stdlib(self):
        print('\nAdding python stdlib')
        src = PREFIX + '/python/Python.framework/Versions/Current/lib/python' + self.py_ver
        dest = join(self.resources_dir, 'Python', 'lib', 'python' + self.py_ver)
        os.makedirs(dest)
        for x in os.listdir(src):
            if x in ('site-packages', 'config', 'test', 'lib2to3', 'lib-tk',
                     'lib-old', 'idlelib', 'plat-mac', 'plat-darwin',
                     'site.py', 'distutils', 'turtledemo', 'tkinter'):
                continue
            x = join(src, x)
            if os.path.isdir(x):
                self.add_package_dir(x, dest)
            elif os.path.splitext(x)[1] in ('.so', '.py'):
                shutil.copy2(x, dest)
                dest2 = join(dest, basename(x))
                if dest2.endswith('.so'):
                    self.fix_dependencies_in_lib(dest2)

    @flush
    def remove_bytecode(self, dest):
        for x in os.walk(dest):
            root = x[0]
            for f in x[-1]:
                if os.path.splitext(f) == '.pyc':
                    os.remove(join(root, f))

    @flush
    def compile_py_modules(self):
        print('\nCompiling Python modules')
        self.remove_bytecode(join(self.resources_dir, 'Python'))
        py_compile(join(self.resources_dir, 'Python'))
        self.remove_bytecode(join(self.frameworks_dir, 'kitty'))
        py_compile(join(self.frameworks_dir, 'kitty'))

    @flush
    def copy_site(self):
        base = os.path.dirname(__file__)
        shutil.copy2(
            join(base, 'site.py'),
            join(self.resources_dir, 'Python', 'lib', 'python' + self.py_ver))

    @flush
    def makedmg(self, d, volname, internet_enable=True, format='UDBZ'):
        ''' Copy a directory d into a dmg named volname '''
        print('\nSigning...')
        sys.stdout.flush()
        destdir = os.path.join(SW, 'dist')
        try:
            shutil.rmtree(destdir)
        except EnvironmentError as err:
            if err.errno != errno.ENOENT:
                raise
        os.mkdir(destdir)
        dmg = os.path.join(destdir, volname + '.dmg')
        if os.path.exists(dmg):
            os.unlink(dmg)
        tdir = tempfile.mkdtemp()
        appdir = os.path.join(tdir, os.path.basename(d))
        shutil.copytree(d, appdir, symlinks=True)
        if self.sign_installers:
            with timeit() as times:
                sign_app(appdir)
            print('Signing completed in %d minutes %d seconds' % tuple(times))
        os.symlink('/Applications', os.path.join(tdir, 'Applications'))
        size_in_mb = int(
            subprocess.check_output(['du', '-s', '-k', tdir]).decode('utf-8')
            .split()[0]) / 1024.
        cmd = [
            '/usr/bin/hdiutil', 'create', '-srcfolder', tdir, '-volname',
            volname, '-format', format
        ]
        if 190 < size_in_mb < 250:
            # We need -size 255m because of a bug in hdiutil. When the size of
            # srcfolder is close to 200MB hdiutil fails with
            # diskimages-helper: resize request is above maximum size allowed.
            cmd += ['-size', '255m']
        print('\nCreating dmg...')
        with timeit() as times:
            subprocess.check_call(cmd + [dmg])
            if internet_enable:
                subprocess.check_call(
                    ['/usr/bin/hdiutil', 'internet-enable', '-yes', dmg])
        print('dmg created in %d minutes and %d seconds' % tuple(times))
        shutil.rmtree(tdir)
        size = os.stat(dmg).st_size / (1024 * 1024.)
        print('\nInstaller size: %.2fMB\n' % size)
        return dmg


def main(args, build_dir):
    Freeze(
        build_dir,
        dont_strip=args.dont_strip,
        sign_installers=args.sign_installers)
