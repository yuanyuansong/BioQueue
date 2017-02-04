#!/usr/bin/env python
from multiprocessing import cpu_count
import baseDriver
import time
import os
import parameterParser
import django_initial
import HTMLParser
import checkPoint
from ui.models import Queue, Protocol, References, Training
import threading
import subprocess
import psutil


settings = baseDriver.get_all_config()
CPU_POOL, MEMORY_POOL, DISK_POOL = baseDriver.get_init_resource()
MAX_JOB = cpu_count()
JOB_TABLE = dict()
USER_REFERENCES = dict()
JOB_PARAMETERS = dict()
JOB_COMMAND = dict()
RUN_PARAMETERS = dict()
JOB_INPUT_FILES = dict()
LAST_OUTPUT_STRING = dict()
OUTPUTS = dict()
OUTPUT_DICT = dict()
NEW_FILES = dict()
LAST_OUTPUT = dict()
INPUT_SIZE = dict()
OUTPUT_SIZE = dict()
FOLDER_SIZE_BEFORE = dict()
CUMULATIVE_OUTPUT_SIZE = dict()
RESOURCES = dict()
LATEST_JOB_ID = 0
LATEST_JOB_STEP = 0
root_path = os.path.split(os.path.realpath(__file__))[0]


def get_steps(protocol_id):
    """
    Get steps of a protocol
    :param protocol_id: int, protocol id
    :return: list, list of unresolved steps
    """
    step_list = []

    steps = Protocol.objects.filter(parent=protocol_id)
    html_parser = HTMLParser.HTMLParser()

    for index, step in enumerate(steps):
        step_list.append({
            'id': index,
            'parameter': html_parser.unescape(str(step.software).rstrip() + " " + str(step.parameter)),
            'specify_output': step.specify_output,
            'hash': step.hash,
        })
    return step_list


def get_user_reference(user):
    """
    Get user references
    :param user: int, user id
    :return: None, the references will be stored directly into the USER_REFERENCES dictionary
    """
    references = References.objects.filter(user_id=user)
    for reference in references:
        if user not in USER_REFERENCES.keys():
            USER_REFERENCES[user] = dict()
        if reference.name in USER_REFERENCES[user]:
            continue
        else:
            USER_REFERENCES[user][reference.name] = reference.path


def create_user_folder(uf, jf):
    """
    Create user folder
    :param uf: string, path to user's folder
    :param jf: string, path to job's folder
    :return: None
    """
    try:
        if not os.path.exists(uf):
            os.mkdir(uf)
        if not os.path.exists(jf):
            os.mkdir(jf)
    except Exception, e:
        print e


def prepare_workspace(resume, run_folder, job_id, user_id, result=''):
    """
    Build path info for the execution of a job
    :param resume: int, if resume equals to 1, BioQueue will use the old folder instead of creating a new one
    :param run_folder: string, parent folder
    :param job_id: int, job id
    :param user_id: int, user id
    :param result: string, folder name for the job
    :return: tuple, path to user folder and job folder
    """
    if resume == -1:
        result_store = baseDriver.rand_sig() + str(job_id)
        user_folder = os.path.join(run_folder, str(user_id))
        run_folder = os.path.join(user_folder, result_store)
        create_user_folder(user_folder, run_folder)
        baseDriver.update(settings['datasets']['job_db'], job_id, 'result', result_store)
    else:
        result_store = result
        user_folder = os.path.join(run_folder, str(user_id))
        run_folder = os.path.join(user_folder, result_store)
    return user_folder, run_folder


def initialize_job_parameters(job_parameter, input_file, user, job_id):
    """
    Parse reference and job parameter (special and input files)
    :param job_parameter: list, job parameter
    :param input_file: list, input files
    :param user: int, user id
    :param job_id: int, job id
    :return: None
    """
    JOB_PARAMETERS[job_id] = parameterParser.build_special_parameter_dict(job_parameter)
    get_user_reference(user)
    if user in USER_REFERENCES.keys():
        JOB_PARAMETERS[job_id] = dict(JOB_PARAMETERS[job_id], **USER_REFERENCES[user])
    JOB_INPUT_FILES[job_id] = input_file.split(';')
    INPUT_SIZE[job_id] = 0


def get_job(max_fetch=1):
    """
    Get job information and store into JOB_TABLE
    :param max_fetch: int, the amount of records to fetch
    :return: None
    """
    # fetch jobs
    jobs = Queue.objects.filter(status=0)[:max_fetch]
    for job in jobs:
        if job in JOB_TABLE.keys():
            continue
        else:
            user_folder, job_folder = prepare_workspace(job.resume, job.run_dir, job.id, job.user_id, job.result)
            initialize_job_parameters(job.parameter, job.input_file, job.user_id, job.id)
            JOB_TABLE[job.id] = {
                'protocol': job.protocol_id,
                'input_file': job.input_file,
                'parameter': job.parameter,
                'run_dir': job.run_dir,
                'result': job.result,
                'status': job.status,
                'user_id': job.user_id,
                'resume': job.resume,
                'steps': get_steps(job.protocol_id),
                'user_folder': user_folder,
                'job_folder': job_folder,
            }
            OUTPUT_DICT[job.id] = dict()
            LAST_OUTPUT[job.id] = []
            CUMULATIVE_OUTPUT_SIZE[job.id] = 0


def get_training_items(step_hash):
    """
    Get the amount of training items
    :param step_hash: string, step hash
    :return: int, the number of training items
    """
    trainings = Training.objects.filter(step=step_hash)
    return len(trainings)


def create_machine_learning_item(step_hash, input_size):
    """
    Create machine learning item and store it into the training table
    :param step_hash: string
    :param input_size: int
    :return: record id
    """
    training = Training(step=step_hash, input=input_size, lock=1)
    training.save()
    return training.id


def finish_job(job_id, error=0):
    """
    Mark a job as finished and release resources it occupied
    If mail notify is switched on, it will send e-mail
    :param job_id: int, job id
    :param error: int, if error occurs, it should be 1
    :return: None
    """
    global CPU_POOL, MEMORY_POOL, DISK_POOL, JOB_TABLE, NEW_FILES, OUTPUTS, OUTPUT_DICT, OUTPUT_SIZE, FOLDER_SIZE_BEFORE, CUMULATIVE_OUTPUT_SIZE, LAST_OUTPUT_STRING
    if job_id in JOB_TABLE.keys():
        if error == 1:
            if settings['mail']['notify'] == 'on':
                try:
                    from notify import MailNotify
                    mail = MailNotify(JOB_TABLE[job_id]['user_id'], 2, job_id, JOB_TABLE[job_id]['protocol'],
                                      JOB_TABLE[job_id]['input_file'], JOB_TABLE[job_id]['parameter'])
                    mail.send_mail(mail.get_user_mail_address(JOB_TABLE[job_id]['user_id']))
                except Exception, e:
                    print e
        else:
            job = Queue.objects.get(id=job_id)
            job.status = -1
            job.save()
            baseDriver.del_output_dict(job_id)
            if settings['mail']['notify'] == 'on':
                try:
                    from notify import MailNotify
                    mail = MailNotify(JOB_TABLE[job_id]['user_id'], 1, job_id, JOB_TABLE[job_id]['protocol'],
                                      JOB_TABLE[job_id]['input_file'], JOB_TABLE[job_id]['parameter'])
                    mail.send_mail(mail.get_user_mail_address(JOB_TABLE[job_id]['user_id']))
                except Exception, e:
                    print e

        if job_id in JOB_TABLE.keys():
            resume = JOB_TABLE[job_id]['resume']
            res_key = str(job_id) + '_' + str(resume + 1)
            if res_key in RESOURCES.keys():
                RESOURCES.pop(res_key)
        DISK_POOL += CUMULATIVE_OUTPUT_SIZE[job_id] - baseDriver.get_folder_size(JOB_TABLE[job_id]['job_folder'])
        JOB_TABLE.pop(job_id)
        if job_id in OUTPUTS.keys():
            OUTPUTS.pop(job_id)
        if job_id in OUTPUT_DICT.keys():
            OUTPUT_DICT.pop(job_id)
        if job_id in LAST_OUTPUT.keys():
            LAST_OUTPUT.pop(job_id)
        if job_id in LAST_OUTPUT_STRING.keys():
            LAST_OUTPUT_STRING.pop(job_id)
        if job_id in CUMULATIVE_OUTPUT_SIZE.keys():
            CUMULATIVE_OUTPUT_SIZE.pop(job_id)


def run_prepare(job_id, job):
    """
    Parse step's parameter and predict the resources needed by the step
    :param job_id: int, jod id
    :param job: dict, job dict
    :return:
    """
    learning = 0

    if job['status'] == -1 and job['resume'] != -1:
        # skip and resume
        OUTPUT_DICT[job_id] = baseDriver.load_output_dict(job_id)

    if (job['resume'] + 1) == len(job['steps']):
        return None
    elif job['status'] > 0:
        return 'running'
    else:
        step = job['steps'][job['resume'] + 1]['parameter']

    step = step.replace('{Job}', str(job_id))

    if job_id in LAST_OUTPUT_STRING.keys():
        step = step.replace('{LastOutput}', LAST_OUTPUT_STRING[job_id])
    if job_id in OUTPUTS.keys():
        step = step.replace('{AllOutputBefore}', ' '.join(OUTPUTS[job_id]))
    if job_id in NEW_FILES.keys():
        step = parameterParser.last_output_map(step, NEW_FILES[job_id])
    if job_id in JOB_PARAMETERS.keys():
        step = parameterParser.special_parameter_map(step, JOB_PARAMETERS[job_id])
    if job_id in OUTPUT_DICT.keys():
        step = parameterParser.output_file_map(step, OUTPUT_DICT[job_id])
    if job_id in JOB_INPUT_FILES.keys():
        step, outside_size = parameterParser.input_file_map(step, JOB_INPUT_FILES[job_id], job['user_folder'])

    step = step.replace('{Workspace}', job['job_folder'])
    step = step.replace('{ThreadN}', str(cpu_count()))
    JOB_COMMAND[job_id] = parameterParser.parameter_string_to_list(step)
    LAST_OUTPUT[job_id] = baseDriver.get_folder_content(job['job_folder'])
    training_num = get_training_items(job['steps'][job['resume'] + 1]['hash'])

    if training_num < 10:
        learning = 1
    if INPUT_SIZE[job_id] == 0:
        INPUT_SIZE[job_id] = baseDriver.get_folder_size(job['job_folder'])
    else:
        if job_id in OUTPUT_SIZE.keys():
            INPUT_SIZE[job_id] = OUTPUT_SIZE[job_id]
        else:
            INPUT_SIZE[job_id] = 0
    FOLDER_SIZE_BEFORE[job_id] = baseDriver.get_folder_size(job['job_folder'])
    INPUT_SIZE[job_id] += outside_size
    resource_needed = checkPoint.predict_resource_needed(job['steps'][job['resume'] + 1]['hash'],
                                                         INPUT_SIZE[job_id],
                                                         training_num)

    if learning == 1:
        trace_id = create_machine_learning_item(job['steps'][job['resume'] + 1]['hash'], INPUT_SIZE[job_id])
        resource_needed['trace'] = trace_id
    return resource_needed


def forecast_step(job_id, step_order, resources):
    """
    Before the running of a step
    :param job_id: int, job id
    :param step_order: int, step order
    :param resources: dictionary, resources required by the step
    :return: If system resources is not enough for the step, it will return False, otherwise, it returns True
    """
    global CPU_POOL, MEMORY_POOL, DISK_POOL, JOB_TABLE
    rollback = 0
    if resources['cpu'] is not None:
        CPU_POOL -= resources['cpu']
        if CPU_POOL < 0:
            rollback = 1
    if resources['mem'] is not None:
        MEMORY_POOL -= resources['mem']
        if MEMORY_POOL < 0:
            rollback = 1
    if resources['disk'] is not None:
        DISK_POOL -= resources['disk']
        if DISK_POOL < 0:
            rollback = 1

    if not rollback:
        job = Queue.objects.get(id=job_id)
        job.status = step_order + 1
        job.save()
        JOB_TABLE[job_id]['status'] = step_order + 1
        return True
    else:
        if resources['cpu'] is not None:
            CPU_POOL += resources['cpu']
        if resources['mem'] is not None:
            MEMORY_POOL += resources['mem']
        if resources['disk'] is not None:
            DISK_POOL += resources['disk']
        return False


def finish_step(job_id, step_order, resources):
    """
    Mark a step as finished
    :param job_id: int, job id
    :param step_order: int, step order
    :param resources: dictionary, resources required by the step
    :return: None
    """
    global CPU_POOL, MEMORY_POOL, DISK_POOL, JOB_TABLE, NEW_FILES, OUTPUTS, OUTPUT_DICT, OUTPUT_SIZE, FOLDER_SIZE_BEFORE, CUMULATIVE_OUTPUT_SIZE, LAST_OUTPUT_STRING
    job = Queue.objects.get(id=job_id)
    job.resume = step_order
    job.status = -2
    try:
        print JOB_TABLE[job_id]
        JOB_TABLE[job_id]['status'] = -2
        print JOB_TABLE[job_id]
        JOB_TABLE[job_id]['resume'] = step_order
        print JOB_TABLE[job_id]
        this_output = baseDriver.get_folder_content(JOB_TABLE[job_id]['job_folder'])
        NEW_FILES[job_id] = sorted(list(set(this_output).difference(set(LAST_OUTPUT[job_id]))))
        NEW_FILES[job_id] = [os.path.join(JOB_TABLE[job_id]['job_folder'], file_name) for file_name in NEW_FILES[job_id]]
    except Exception, e:
        print e

    if job_id in OUTPUTS.keys():
        OUTPUTS[job_id].extend(NEW_FILES[job_id])
    else:
        OUTPUTS[job_id] = NEW_FILES[job_id]

    OUTPUT_DICT[job_id][step_order + 1] = NEW_FILES[job_id]
    LAST_OUTPUT_STRING[job_id] = ' '.join(NEW_FILES[job_id])
    job.save()
    OUTPUT_SIZE[job_id] = baseDriver.get_folder_size(JOB_TABLE[job_id]['job_folder']) - FOLDER_SIZE_BEFORE[job_id]
    CUMULATIVE_OUTPUT_SIZE[job_id] += OUTPUT_SIZE[job_id]
    if resources['cpu'] is not None:
        CPU_POOL += resources['cpu']
    if resources['mem'] is not None:
        MEMORY_POOL += resources['mem']
    if resources['disk'] is not None:
        DISK_POOL += resources['disk']


def error_job(job_id):
    """
    Error occurs
    :param job_id: int, job id
    :return: None
    """
    job = Queue.objects.get(id=job_id)
    job.status = -3
    job.ter = 0
    job.save()
    if job_id in OUTPUT_DICT.keys():
        baseDriver.save_output_dict(OUTPUT_DICT[job_id], job_id)
    finish_job(job_id, 1)


def run_step(job_desc, resources):
    """
    Run step (parallel to main thread)
    :param job_desc: string, jod id + "_" + step order
    :param resources: dictionary, resources required by the step
    :return: None
    """
    global LATEST_JOB_ID, LATEST_JOB_STEP, CPU_POOL, MEMORY_POOL, DISK_POOL
    items = job_desc.split('_')
    job_id = int(items[0])
    step_order = int(items[1])
    recheck = forecast_step(job_id, step_order, resources)
    if settings['cluster']['type']:
        # for cluster
        import clusterSupport
        predict_cpu = int(round(resources['cpu']) / 100)
        if predict_cpu > settings['cluster']['cpu'] or predict_cpu == 0:
            allocate_cpu = settings['cluster']['cpu']
        else:
            allocate_cpu = predict_cpu
        if resources['mem'] > 1073741824:
            allocate_mem = str(int(round(resources['mem'] / 1073741824) + 1)) + 'Gb'
        elif resources['mem']:
            allocate_mem = ''
        else:
            allocate_mem = str(int(round(resources['mem'] / 1048576) + 1)) + 'Mb'

        baseDriver.update(settings['datasets']['job_db'], job_id, 'status', step_order + 1)
        return_code = clusterSupport.main(settings['cluster']['type'], ' '.join(JOB_COMMAND[job_id]),
                                          job_id, step_order, allocate_cpu, allocate_mem,
                                          settings['cluster']['queue'], JOB_TABLE[job_id]['job_folder'])
        if return_code != 0:
            error_job(job_id)
    else:
        # for local environment or cloud
        print "Now run %s" % job_desc
        print CPU_POOL, MEMORY_POOL, DISK_POOL
        try:
            log_file = os.path.join(settings["env"]["log"], str(job_id))
            log_file_handler = open(log_file, "a")
            step_process = subprocess.Popen(JOB_COMMAND[job_id], shell=False, stdout=log_file_handler,
                                            stderr=log_file_handler, cwd=JOB_TABLE[job_id]['job_folder'])
            process_id = step_process.pid
            if 'trace' in resources.keys():
                learn_process = subprocess.Popen(["python", os.path.join(root_path, 'mlCollector.py'),
                                                  "-p", str(step_process.pid), "-n",
                                                  str(JOB_TABLE[job_id]['steps'][step_order]['hash']),
                                                  "-j", str(resources['trace'])], shell=False, stdout=None,
                                                 stderr=subprocess.STDOUT)
            while step_process.poll() is None:
                if process_id in psutil.pids():
                    proc_info = psutil.Process(process_id)
                    if proc_info.is_running():
                        job = Queue.objects.get(id=job_id)
                        if job.ter:
                            job.status = -3
                            job.ter = 0
                            job.save()
                            proc_info.kill()
                            error_job(job_id)
                            if resources['cpu'] is not None:
                                CPU_POOL += resources['cpu']
                            if resources['mem'] is not None:
                                MEMORY_POOL += resources['mem']
                            if resources['disk'] is not None:
                                DISK_POOL += resources['disk']
                            return None

                time.sleep(30)
            log_file_handler.close()
            print "Now job %s finished." % job_desc
            # finish_step(job_id, step_order, resources)
            if step_process.returncode != 0:
                error_job(job_id)
            else:
                finish_step(job_id, step_order, resources)
            JOB_TABLE[job_id]['resume'] = step_order
            if job_id > LATEST_JOB_ID and (step_order + 1) > LATEST_JOB_STEP:
                LATEST_JOB_ID = job_id
                LATEST_JOB_STEP = step_order
        except Exception, e:
            print e
            error_job(job_id)


def set_checkpoint_info(job_id, cause):
    """
    Interact with frontend for checkpoint
    :param job_id: int, job id
    :param cause: int, cause for the suspension
    :return: None
    """
    job = Queue.objects.get(id=job_id)
    job.wait_for = cause
    job.save()


def main():
    global LATEST_JOB_ID, LATEST_JOB_STEP, RESOURCES
    while True:
        cpu_indeed = baseDriver.get_cpu_available()
        mem_indeed = baseDriver.get_memo_usage_available()
        disk_indeed = baseDriver.get_disk_free()
        get_job(MAX_JOB - len(JOB_TABLE))

        sorted_job_info = sorted(JOB_TABLE.keys())
        for job_id in sorted_job_info:
            resource = run_prepare(job_id, JOB_TABLE[job_id])
            if resource is None:
                finish_job(job_id)
                continue
            elif resource == 'running':
                continue
            else:
                previous_step = str(job_id) + '_' + str(JOB_TABLE[job_id]['resume'])
                now_step = str(job_id) + '_' + str(JOB_TABLE[job_id]['resume'] + 1)
                if previous_step in RESOURCES.keys():
                    RESOURCES.pop(previous_step)
                if now_step in RESOURCES.keys():
                    continue
                else:
                    RESOURCES[now_step] = resource

        biggest_cpu = None
        biggest_mem = None
        biggest_id = None

        sorted_resources_info = sorted(RESOURCES.keys())
        for index, job_desc in enumerate(sorted_resources_info):
            items = job_desc.split('_')
            job_id = int(items[0])
            step_order = int(items[1])

            if job_id not in JOB_TABLE.keys():
                continue
            if JOB_TABLE[job_id]['status'] > 0:
                continue

            if RESOURCES[job_desc]['cpu'] is None \
                    and RESOURCES[job_desc]['mem'] is None \
                    and RESOURCES[job_desc]['disk'] is None:
                new_thread = threading.Thread(target=run_step, args=(job_desc, RESOURCES[job_desc]))
                new_thread.setDaemon(True)
                new_thread.start()
                break
            else:
                if RESOURCES[job_desc]['cpu'] > cpu_indeed or RESOURCES[job_desc]['cpu'] > CPU_POOL:
                    set_checkpoint_info(job_id, 3)
                elif RESOURCES[job_desc]['mem'] > mem_indeed or RESOURCES[job_desc]['mem'] > MEMORY_POOL:
                    set_checkpoint_info(job_id, 2)
                elif RESOURCES[job_desc]['disk'] > disk_indeed or RESOURCES[job_desc]['disk'] > DISK_POOL:
                    set_checkpoint_info(job_id, 1)
                else:
                    if biggest_cpu is None:
                        biggest_cpu = RESOURCES[job_desc]['cpu']
                    if biggest_mem is None:
                        biggest_mem = RESOURCES[job_desc]['mem']
                    if biggest_id is None:
                        biggest_id = job_desc

                    if biggest_cpu < RESOURCES[job_desc]['cpu']:
                        biggest_cpu = RESOURCES[job_desc]['cpu']
                        biggest_mem = RESOURCES[job_desc]['mem']
                        biggest_id = job_desc
        if biggest_id is not None:
            new_thread = threading.Thread(target=run_step, args=(biggest_id, RESOURCES[biggest_id]))
            new_thread.setDaemon(True)
            new_thread.start()
        time.sleep(5)


if __name__ == '__main__':
    main()