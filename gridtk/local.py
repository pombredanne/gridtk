#!/usr/bin/env python
# vim: set fileencoding=utf-8 :
# Andre Anjos <andre.anjos@idiap.ch>
# Wed 24 Aug 2011 13:06:25 CEST

"""Defines the job manager which can help you managing submitted grid jobs.
"""

from __future__ import print_function

import subprocess
import time
import copy, os, sys

if sys.version_info[0] >= 3:
  from pickle import dumps, loads
else:
  from cPickle import dumps, loads

from .tools import makedirs_safe, logger, str_


from .manager import JobManager
from .models import add_job, Job

class JobManagerLocal(JobManager):
  """Manages jobs run in parallel on the local machine."""
  def __init__(self, **kwargs):
    """Initializes this object with a state file and a method for qsub'bing.

    Keyword parameters:

    statefile
      The file containing a valid status database for the manager. If the file
      does not exist it is initialized. If it exists, it is loaded.

    """
    JobManager.__init__(self, **kwargs)


  def submit(self, command_line, name = None, array = None, dependencies = [], log_dir = None, dry_run = False, stop_on_failure = False, **kwargs):
    """Submits a job that will be executed on the local machine during a call to "run".
    All kwargs will simply be ignored."""
    # add job to database
    self.lock()
    job = add_job(self.session, command_line=command_line, name=name, dependencies=dependencies, array=array, log_dir=log_dir, stop_on_failure=stop_on_failure)
    logger.info("Added job '%s' to the database" % job)

    if dry_run:
      print("Would have added the Job", job, "to the database to be executed locally.")
      self.session.delete(job)
      logger.info("Deleted job '%s' from the database due to dry-run option" % job)
      job_id = None
    else:
      job_id = job.id

    # return the new job id
    self.unlock()
    return job_id


  def resubmit(self, job_ids = None, failed_only = False, running_jobs = False):
    """Re-submit jobs automatically"""
    self.lock()
    # iterate over all jobs
    jobs = self.get_jobs(job_ids)
    accepted_old_status = ('failure',) if failed_only else ('success', 'failure')
    for job in jobs:
      # check if this job needs re-submission
      if running_jobs or job.status in accepted_old_status:
        # re-submit job to the grid
        logger.info("Re-submitted job '%s' to the database" % job)
        job.submit('local')

    self.session.commit()
    self.unlock()


  def stop_jobs(self, job_ids=None):
    """Resets the status of the job to 'submitted' when they are labeled as 'executing'."""
    self.lock()

    jobs = self.get_jobs(job_ids)
    for job in jobs:
      if job.status in ('executing', 'queued', 'waiting'):
        logger.info("Reset job '%s' in the database" % job.name)
        job.submit()

    self.session.commit()
    self.unlock()

  def stop_job(self, job_id, array_id = None):
    """Resets the status of the given to 'submitted' when they are labeled as 'executing'."""
    self.lock()

    job, array_job = self._job_and_array(job_id, array_id)
    if job is not None:
      if job.status in ('executing', 'queued', 'waiting'):
        logger.info("Reset job '%s' in the database" % job.name)
        job.status = 'submitted'

      if array_job is not None and array_job.status in ('executing', 'queued', 'waiting'):
        logger.debug("Reset array job '%s' in the database" % array_job)
        array_job.status = 'submitted'
      if array_job is None:
        for array_job in job.array:
          if array_job.status in ('executing', 'queued', 'waiting'):
            logger.debug("Reset array job '%s' in the database" % array_job)
            array_job.status = 'submitted'

    self.session.commit()
    self.unlock()


#####################################################################
###### Methods to run the jobs in parallel on the local machine #####

  def _run_parallel_job(self, job_id, array_id = None, no_log = False):
    """Executes the code for this job on the local machine."""
    environ = copy.deepcopy(os.environ)
    environ['JOB_ID'] = str(job_id)
    if array_id:
      environ['SGE_TASK_ID'] = str(array_id)
    else:
      environ['SGE_TASK_ID'] = 'undefined'

    # generate call to the wrapper script
    command = [self.wrapper_script, '-ld', self._database, 'run-job']

    job, array_job = self._job_and_array(job_id, array_id)
    logger.info("Starting execution of Job '%s': '%s'" % (self._format_log(job_id, array_id, len(job.array)), job.name))
    # create log files
    if no_log or job.log_dir is None:
      out, err = sys.stdout, sys.stderr
    else:
      makedirs_safe(job.log_dir)
      # create line-buffered files for writing output and error status
      if array_job is not None:
        out, err = open(array_job.std_out_file(), 'w', 1), open(array_job.std_err_file(), 'w', 1)
      else:
        out, err = open(job.std_out_file(), 'w', 1), open(job.std_err_file(), 'w', 1)

    # return the subprocess pipe to the process
    try:
      return subprocess.Popen(command, env=environ, stdout=out, stderr=err, bufsize=1)
    except OSError as e:
      logger.error("Could not execute job '%s' locally,\nreason:\t%s,\ncommand_line\t%s:" % (self._format_log(job_id, array_id, len(job.array)), e, job.get_command_line()))
      job.finish(117, array_id) # ASCII 'O'
      return None


  def _format_log(self, job_id, array_id = None, array_count = 0):
    return ("%d (%d/%d)" % (job_id, array_id, array_count)) if array_id is not None and array_count else ("%d (%d)" % (job_id, array_id)) if array_id is not None else ("%d" % job_id)

  def run_scheduler(self, parallel_jobs = 1, job_ids = None, sleep_time = 0.1, die_when_finished = False, no_log = False):
    """Starts the scheduler, which is constantly checking for jobs that should be ran."""
    running_tasks = []
    try:

      # keep the scheduler alive until every job is finished or the KeyboardInterrupt is caught
      while True:
        # Flag that might be set in some rare cases, and that prevents the scheduler to die
        repeat_execution = False
        # FIRST, try if there are finished processes; this does not need a lock
        for task_index in range(len(running_tasks)-1, -1, -1):
          task = running_tasks[task_index]
          process = task[0]

          if process.poll() is not None:
            # process ended
            job_id = task[1]
            array_id = task[2] if len(task) > 2 else None
            self.lock()
            job, array_job = self._job_and_array(job_id, array_id)
            if array_job: job = array_job
            result = "%s (%d)" % (job.status, job.result)
            self.unlock()
            logger.info("Job '%s' finished execution with result %s" % (self._format_log(job_id, array_id), result))
            # in any case, remove the job from the list
            del running_tasks[task_index]
        # SECOND, check if new jobs can be submitted; THIS NEEDS TO LOCK THE DATABASE
        if len(running_tasks) < parallel_jobs:
          # get all unfinished jobs:
          self.lock()
          jobs = self.get_jobs(job_ids)
          # put all new jobs into the queue
          for job in jobs:
            if job.status == 'submitted':
              job.queue()

          # get all unfinished jobs that are submitted to the local queue
          unfinished_jobs = [job for job in jobs if job.status in ('queued', 'executing') and job.queue_name == 'local']
          for job in unfinished_jobs:
            if job.array:
              # find array jobs that can run
              queued_array_jobs = [array_job for array_job in job.array if array_job.status == 'queued']
              if not len(queued_array_jobs):
                job.finish(0, -1)
                repeat_execution = True
              else:
                # there are new array jobs to run
                for i in range(min(parallel_jobs - len(running_tasks), len(queued_array_jobs))):
                  array_job = queued_array_jobs[i]
                  # start a new job from the array
                  process = self._run_parallel_job(job.id, array_job.id, no_log=no_log)
                  if process is None:
                    continue
                  running_tasks.append((process, job.id, array_job.id))
                  # we here set the status to executing manually to avoid jobs to be run twice
                  # e.g., if the loop is executed while the asynchronous job did not start yet
                  array_job.status = 'executing'
                  job.status = 'executing'
                  if len(running_tasks) == parallel_jobs:
                    break
            else:
              if job.status == 'queued':
                # start a new job
                process = self._run_parallel_job(job.id, no_log=no_log)
                if process is None:
                  continue
                running_tasks.append((process, job.id))
                # we here set the status to executing manually to avoid jobs to be run twice
                # e.g., if the loop is executed while the asynchronous job did not start yet
                job.status = 'executing'
            if len(running_tasks) == parallel_jobs:
              break

          self.session.commit()
          self.unlock()

        # if after the submission of jobs there are no jobs running, we should have finished all the queue.
        if die_when_finished and not repeat_execution and len(running_tasks) == 0:
          logger.info("Stopping task scheduler since there are no more jobs running.")
          break

        # THIRD: sleep the desired amount of time before re-checking
        time.sleep(sleep_time)

    # This is the only way to stop: you have to interrupt the scheduler
    except KeyboardInterrupt:
      if hasattr(self, 'session'):
        self.unlock()
      logger.info("Stopping task scheduler due to user interrupt.")
      for task in running_tasks:
        logger.warn("Killing job '%s' that was still running." % self._format_log(task[1], task[2] if len(task) > 2 else None))
        task[0].kill()
        self.stop_job(task[1])
      # stopp all jobs that are currently running or queued
      self.stop_jobs()
