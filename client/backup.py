#!/bin/env python3
#!/opt/local/bin/python3.3

# Copyright (c) 2015-2016 Lars Hupfeldt Nielsen, Hupfeldt IT
# All rights reserved. This work is under a BSD license, see LICENSE.TXT.


import sys, os, shutil
from os.path import join as jp
import subprocess, time, resource

from . import notifications
from .singleton_script import singleton_script
from .rotate_logs import rotate_logs
from .config_objects import app_dirs


sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, app_dirs.user_config_dir)


def _check_cfg_file(cfg_file_name, cfg_file_example_name, msg=''):
    cfg_file = jp(app_dirs.user_config_dir, cfg_file_name)
    if not os.path.exists(cfg_file):
        print("*** Error: Configuration file '{cf}' not found".format(cf=cfg_file), file=sys.stderr)
        here = os.path.dirname(__file__)
        cfg_template_src = jp(here, 'config', cfg_file_example_name)
        cfg_template_tgt = jp(app_dirs.user_config_dir, cfg_file_example_name)
        try:
            os.mkdir(app_dirs.user_config_dir)
        except FileExistsError:
            pass
        shutil.copy(cfg_template_src, app_dirs.user_config_dir)
        print("  You can rename '{tmplt}' to '{cf}' and edit it.".format(cf=cfg_file, tmplt=cfg_template_tgt), file=sys.stderr)
        print("  " + msg + '\n', file=sys.stderr)
        return 0

    return 1


cfg_found = 0

try:
    from bbmt_config import config  # pylint: disable=wrong-import-order,no-name-in-module
    cfg_found += 1
except ImportError as ex:
    cfg_file_name = 'bbmt_config.py'
    cfg_template_file_name = cfg_file_name + '.template'
    cfg_found += _check_cfg_file(cfg_file_name, cfg_template_file_name)
    if cfg_found == 1:
        raise


exclude_from_file_name = 'user_file_selection.conf'
exclude_from_file_example = exclude_from_file_name + '.example'
cfg_found += _check_cfg_file(
    exclude_from_file_name, exclude_from_file_example, "Make sure to review it closely, so that you don not exclude anything you want to backup.")

if cfg_found < 2:
    sys.exit(1)

exclude_from_file = jp(app_dirs.user_config_dir, exclude_from_file_name)

program_name = 'Backup'


def message(msg, use_notify=True):
    if sys.stdout.isatty():
        print(msg)
    with config.log_file.open('a+') as log_file:
        print(msg, file=log_file)
    if use_notify:
        notifications.notify(program_name, msg, notifications.STOCK_DIALOG_INFO, expire_timeout=10000)


def error(msg):
    err = "*** ERROR: "
    print(err, msg, file=sys.stderr)
    check_log_file_msg = "Check log file: '{!s}'".format(config.log_file)
    print(check_log_file_msg, file=sys.stderr)
    with config.log_file.open('a+') as log_file:
        print(err, msg, file=log_file)
    msg += " " + check_log_file_msg
    notifications.notify(program_name, msg, notifications.STOCK_DIALOG_ERROR)


def borg(args):
    os.environ['BORG_RSH'] = "ssh -i " + str(config.ssh_key)
    os.environ['BORG_PASSPHRASE'] = config.passphrase

    cmd = [config.borg]
    cmd.extend(args)
    cmd.append('-v')
    message(' '.join(cmd), use_notify=False)

    if not sys.stdout.isatty():
        with config.log_file.open('a+') as log_file:
            subprocess.check_call(cmd, stdout=log_file, stderr=log_file)
    else:
        subprocess.check_call(cmd)


def backup(from_dir, remote_url, prefix):
    fdrel = lambda path : jp(from_dir, path)

    # Options for excluding files
    excl = []
    try:
        # If DOWNLOAD dir does not exist xdg-user-dir will return home dir, make sure we don't exclude it
        download_dir = os.path.normpath(subprocess.check_output(['xdg-user-dir', 'DOWNLOAD']).decode('unicode_escape').strip())
        if not download_dir in (os.path.normpath(from_dir), os.path.normpath(os.path.expanduser("~"))):
            excl.append(download_dir)
    except FileNotFoundError:
        excl.extend([fdrel('Downloads')])

    excl = ["--exclude=sh:" + fdrel(dd) for dd in excl]

    # Start backup
    borg(['create', '--stats', '--lock-wait', '300', '--show-rc', '--progress', '--compression', 'lz4',
          '--exclude-from', exclude_from_file, '--exclude-caches'] + excl +
         [remote_url + '::' + prefix + '-' + time.strftime("%Y-%m-%d:%H.%M.%S"), from_dir])


def prune(remote_url, prefix, keep_within, keep_hourly, keep_daily, keep_weekly, keep_monthly, keep_yearly):
    borg(['prune', '--lock-wait', '300', '--show-rc',
          #  --save-space
          '--prefix', prefix,
          '--keep-within', keep_within,
          '--keep-hourly', str(keep_hourly),
          '--keep-daily', str(keep_daily),
          '--keep-weekly', str(keep_weekly),
          '--keep-monthly', str(keep_monthly),
          '--keep-yearly', str(keep_yearly),
          remote_url])


def all_backups():
    notifications.init(program_name, ignore_errors=True)

    try:
        for dd in reversed(list(config.log_file.parents)):
            os.makedirs(str(dd), exist_ok=True)
    except FileExistsError:
        # os.makedirs still raises FileExistsError if mode is not as expected
        assert os.path.isdir(config.log_dir)

    message("Starting backups")

    rotate_logs(config.log_file)
    singleton_script()
    resource.setrlimit(resource.RLIMIT_NOFILE, (1024, 1024))

    failed = []
    for backup_rule in config.backup_rules.values():
        remote_url = backup_rule.target_user + '@' + backup_rule.target_host + ':backup'
        try:
            message("Backing up " + repr(backup_rule.from_dir) + " to " + repr(remote_url))
            backup(backup_rule.from_dir, remote_url, backup_rule.prefix)
            message("Cleaning up at " + repr(remote_url))
            prune(remote_url, backup_rule.prefix,
                  backup_rule.keep_within,
                  backup_rule.keep_hourly, backup_rule.keep_daily, backup_rule.keep_weekly, backup_rule.keep_monthly, backup_rule.keep_yearly)
            message("Successfully backed up " + repr(backup_rule.from_dir) + " to " + repr(remote_url))
        except subprocess.CalledProcessError as ex:
            error(str(ex) + ". Backup (or cleanup) to " + repr(remote_url) + " failed!")
            failed.append(remote_url)

    if failed:
        msg = "ALL backups (or all cleanups) failed" if len(failed) == len(config.backup_rules) else "Some backups (or cleanups) failed"
        raise Exception(msg + ": " + str(failed))

    message("All Successful")
    # TODO leave notifications with a timeout if possible and don't sleep!
    time.sleep(2 if sys.stdout.isatty() else 60)
    notifications.clear()


def main():
    try:
        all_backups()
    except Exception as ex:
        error(str(ex))
        raise


if __name__ == "__main__":
    main()
