import os
import subprocess
import sys
import packtivity.utils as utils
import packtivity.logutils as logutils
import time
import psutil
import click
import yaml
import shlex

handlers,environment = utils.handler_decorator()

def sourcepath(path):
    if 'PACKTIVITY_WORKDIR_LOCATION' in os.environ:
        old,new = os.environ['PACKTIVITY_WORKDIR_LOCATION'].split(':')
        dockerpath = new+path.rsplit(old,1)[1]
        return dockerpath
    else:
        return path

def cvmfs_from_volume_plugin(command_line,cvmfs_repos = None):
    if not cvmfs_repos:
        cvmfs_repos = yaml.load(os.environ.get('PACKTIVITY_CVMFS_REPOS','null'))
    if not cvmfs_repos:
        cvmfs_repos  = ['atlas.cern.ch','atlas-condb.cern.ch','sft.cern.ch']
    command_line += ' --security-opt label:disable'
    for repo in cvmfs_repos:
        command_line += ' --volume-driver cvmfs -v {cvmfs_repo}:/cvmfs/{cvmfs_repo}'.format(cvmfs_repo = repo)
    return command_line

def cvmfs_from_external_mount(command_line):
    command_line+=' -v {}:/cvmfs'.format(os.environ.get('PACKTIVITY_CVMFS_LOCATION','/cvmfs'))
    return command_line

def prepare_docker(context,do_cvmfs,do_auth,log):
    nametag = context['nametag']
    metadir  = context['metadir']
    readwrites  = context['readwrite']
    readonlies = context['readonly']

    docker_mod = ''
    for rw in readwrites:
        docker_mod += '-v {}:{}:rw'.format(sourcepath(os.path.abspath(rw)),rw)
    for ro in readonlies:
        docker_mod += ' -v {}:{}:ro'.format(sourcepath(ro),ro)

    if do_cvmfs:
        cvmfs_source = os.environ.get('PACKTIVITY_CVMFS_SOURCE','external')
        if cvmfs_source == 'external':
            docker_mod = cvmfs_from_external_mount(docker_mod)
        elif cvmfs_source == 'voldriver':
            docker_mod = cvmfs_from_volume_plugin(docker_mod)
        else:
            raise RuntimeError('unknown CVMFS location requested')

    if do_auth:
        if 'PACKTIVITY_AUTH_LOCATION' not in os.environ:
            docker_mod+=' -v /home/recast/recast_auth:/recast_auth'
        else:
            docker_mod+=' -v {}:/recast_auth'.format(os.environ['PACKTIVITY_AUTH_LOCATION'])

    cidfile = '{}/{}.cid'.format(metadir,nametag)

    if os.path.exists(cidfile):
        log.warning('cid file %s seems to exist, docker run will crash',cidfile)
    docker_mod += ' --cidfile {}'.format(cidfile)

    return docker_mod


def prepare_docker_context(context,environment,log):
    container = environment['image']
    report = '''\n\
--------------
run in docker container: {container}
with env: {env}
resources: {resources}
--------------
    '''.format(container = container,
               env = environment['envscript'] if environment['envscript'] else 'default env',
               resources = environment['resources']
              )
    log.debug(report)
    
    do_cvmfs = 'CVMFS' in environment['resources']
    do_auth  = ('GRIDProxy'  in environment['resources']) or ('KRB5Auth' in environment['resources'])
    log.debug('do_auth: %s do_cvmfs: %s',do_auth,do_cvmfs)
    
    
    
    docker_mod = prepare_docker(context,do_cvmfs,do_auth,log)
    return docker_mod

def run_docker_with_script(context,environment,job,log):
    image = environment['image']
    imagetag = environment['imagetag']
    nametag = context['nametag']
    
    script = job['script']
    interpreter = job['interpreter']
    
    do_cvmfs = 'CVMFS' in environment['resources']
    log.debug('script is:')
    log.debug('\n--------------\n'+script+'\n--------------')
    docker_mod = prepare_docker_context(context,environment,log)
    if 'PACKTIVITY_DRYRUN' in os.environ:
        return
        
    indocker = interpreter
    envmod = 'source {} && '.format(environment['envscript']) if environment['envscript'] else ''
    indocker = envmod+indocker
    
    try:
        runlog = logutils.setup_logging_topic(nametag,context,'run', return_logger = True)

        if do_cvmfs:
            if 'PACKTIVITY_WITHIN_DOCKER' not in os.environ:
                subprocess.check_call('cvmfs_config probe')
                
        subcmd = 'docker run --rm -i {docker_mod} {image}:{imagetag} sh -c \'{indocker}\' '.format(image = image, imagetag = imagetag, docker_mod = docker_mod, indocker = indocker)
        log.debug('running docker cmd: %s',subcmd)
        proc = subprocess.Popen(shlex.split(subcmd), stdin = subprocess.PIPE, stderr = subprocess.STDOUT, stdout = subprocess.PIPE, bufsize=1)

        log.debug('started run subprocess with pid %s. now piping script',proc.pid)
        proc.stdin.write(script)
        proc.stdin.close()
        time.sleep(0.5)

        for line in iter(proc.stdout.readline, ''):
            runlog.info(line.strip())
        while proc.poll() is None:
            pass

        log.debug('docker run subprocess finished. return code: %s',proc.returncode)
        if proc.returncode:
            log.error('non-zero return code raising exception')
            raise subprocess.CalledProcessError(returncode =  proc.returncode, cmd = subcmd)
        log.debug('moving on from run')
    except subprocess.CalledProcessError as exc:
        log.exception('subprocess failed. code: %s,  command %s',exc.returncode,exc.cmd)
        raise RuntimeError('failed docker run subprocess in docker_enc_handler.')
    except:
        log.exception("Unexpected error: %s",sys.exc_info())
        raise
    finally:
        log.debug('finally for run')

def prepare_full_docker_with_oneliner(context,environment,command,log):
    image = environment['image']
    imagetag = environment['imagetag']
    do_cvmfs = 'CVMFS' in environment['resources']
    
    report = '''\n\
--------------
running one liner in container.
command: {command}
--------------
    '''.format(command = command)
    log.debug(report)
    
    docker_mod = prepare_docker_context(context,environment,log)
    
    envmod = 'source {} &&'.format(environment['envscript']) if environment['envscript'] else ''
    in_docker_cmd = '{envmodifier} {command}'.format(envmodifier = envmod, command = command)
    
    fullest_command = 'docker run --rm {docker_mod} {image}:{imagetag} sh -c \'{in_dock}\''.format(
                        docker_mod = docker_mod,
                        image = image,
                        imagetag = imagetag,
                        in_dock = in_docker_cmd
                        )
    
    if do_cvmfs:
        if 'PACKTIVITY_WITHIN_DOCKER' not in os.environ:
            fullest_command = 'cvmfs_config probe && {}'.format(fullest_command)
    return fullest_command

def docker_pull(docker_pull_cmd,log,context,nametag):
    log.debug('docker pull command: \n  %s',docker_pull_cmd)
    if 'PACKTIVITY_DRYRUN' in os.environ:
        return
    try:
        pulllog = logutils.setup_logging_topic(nametag,context,'pull', return_logger = True)
        proc = subprocess.Popen(shlex.split(docker_pull_cmd), stderr = subprocess.STDOUT, stdout = subprocess.PIPE, bufsize=1)
        log.debug('started pull subprocess with pid %s. now wait to finish',proc.pid)
        time.sleep(0.5)
        log.debug('process children: %s',[x for x in psutil.Process(proc.pid).children(recursive = True)])

        for line in iter(proc.stdout.readline, ''):
            pulllog.info(line.strip())
        while proc.poll() is None:
            pass

        log.debug('pull subprocess finished. return code: %s',proc.returncode)
        if proc.returncode:
            log.error('non-zero return code raising exception')
            raise subprocess.CalledProcessError(returncode =  proc.returncode, cmd = docker_pull_cmd)
        log.debug('moving on from pull')
    except RuntimeError as e:
        log.exception('caught RuntimeError')
        raise e
    except subprocess.CalledProcessError as exc:
        log.exception('subprocess failed. code: %s,  command %s',exc.returncode,exc.cmd)
        raise RuntimeError('failed docker pull subprocess in docker_enc_handler.')
    except:
        log.exception("Unexpected error: %s",sys.exc_info())
        raise
    finally:
        log.debug('finally for pull')

def docker_run_cmd(fullest_command,log,context,nametag):
    log.debug('docker run  command: \n%s',fullest_command)
    if 'PACKTIVITY_DRYRUN' in os.environ:
        return
    try:
        runlog = logutils.setup_logging_topic(nametag,context,'run', return_logger = True)
        proc = subprocess.Popen(shlex.split(fullest_command), stderr = subprocess.STDOUT, stdout = subprocess.PIPE, bufsize=1)
        log.debug('started run subprocess with pid %s. now wait to finish',proc.pid)
        time.sleep(0.5)
        log.debug('process children: %s',[x for x in psutil.Process(proc.pid).children(recursive = True)])

        for line in iter(proc.stdout.readline, ''):
            runlog.info(line.strip())
        while proc.poll() is None:
            pass

        log.debug('docker run subprocess finished. return code: %s',proc.returncode)
        if proc.returncode:
            log.error('non-zero return code raising exception')
            raise subprocess.CalledProcessError(returncode =  proc.returncode, cmd = fullest_command)
        log.debug('moving on from run')
    except subprocess.CalledProcessError as exc:
        log.exception('subprocess failed. code: %s,  command %s',exc.returncode,exc.cmd)
        raise RuntimeError('failed docker run subprocess in docker_enc_handler.')
    except:
        log.exception("Unexpected error: %s",sys.exc_info())
        raise
    finally:
        log.debug('finally for run')


@environment('docker-encapsulated')
def docker_enc_handler(environment,context,job):
    nametag = context['nametag']
    log  = logutils.setup_logging_topic(nametag,context,'step',return_logger = True)
    
    # short interruption to create metainfo storage location
    metadir  = '{}/_packtivity'.format(context['readwrite'][0])
    context['metadir'] = metadir
    log.info('creating metadirectory %s if necessary. exists? : %s',metadir,os.path.exists(metadir))
    utils.mkdir_p(metadir)
    
    #setup more detailed logging
    logutils.setup_logging(nametag, context)
    
    log.debug('starting log for step: %s',nametag)
    if 'PACKTIVITY_DOCKER_NOPULL' not in os.environ:
        log.info('prepare pull')
        docker_pull_cmd = 'docker pull {container}:{tag}'.format(
            container = environment['image'],
            tag = environment['imagetag']
        )
        docker_pull(docker_pull_cmd,log,context,nametag)
        
    log.info('running job')
    
    if 'command' in job:
        # log.info('running oneliner command')
        docker_run_cmd_str = prepare_full_docker_with_oneliner(context,environment,job['command'],log)
        docker_run_cmd(docker_run_cmd_str,log,context,nametag)
        log.debug('reached return for docker_enc_handler')
    elif 'script' in job:
        run_docker_with_script(context,environment,job,log)
    else:
        raise RuntimeError('do not know yet how to run this...')

@environment('noop-env')
def noop_env(environment,context,job):
    nametag = context['nametag']
    log  = logutils.setup_logging_topic(nametag,context,'step',return_logger = True)
    log.info('context is: %s',context)
    log.info('would be running this job: %s',job)

@environment('localproc-env')
def localproc_env(environment,context,job):
    nametag = context['nametag']
    log  =  logutils.setup_logging_topic(nametag,context,'step',return_logger = True)
    olddir = os.path.realpath(os.curdir)
    workdir = context['readwrite'][0]
    log.info('running local command %s',job['command'])
    try:
        log.info('changing to workdirectory %s',workdir)
        utils.mkdir_p(workdir)
        os.chdir(workdir)
        #this is used for testing and we will keep this shell
        #doesn't make sense to wrap in sh ...
        subprocess.check_call(job['command'], shell = True)
    except:
        log.exception('local job failed. job: %s',job)
        raise
    finally:
        log.info('changing back to original directory %s',olddir)
        os.chdir(olddir)

@environment('manual-env')
def manual_env(environment,context,job):
    instructions = environment['instructions']
    ctx = yaml.safe_dump(context,default_flow_style = False)
    click.secho(instructions, fg = 'blue')
    click.secho(ctx, fg = 'cyan')
