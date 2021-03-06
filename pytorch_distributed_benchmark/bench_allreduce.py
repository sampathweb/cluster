#!/usr/bin/env python
#
# Launching simple MPI job on AWS cluster or locally

# Amazon Linux product page:
# https://aws.amazon.com/marketplace/fulfillment?productId=f3afce45-648d-47d7-9f6c-1ec273f7df70&ref_=dtl_psb_continue
# Ubuntu page:
# https://aws.amazon.com/marketplace/fulfillment?productId=17364a08-2d77-4969-8dbe-d46dcfea4d64&ref_=dtl_psb_continue



import os
import sys
import time
import torch
import torch.distributed as dist
from torch.multiprocessing import Process

import argparse

parser = argparse.ArgumentParser(description='launch')

# launcher flags

parser.add_argument('--zone', type=str, default='', help=("which AWS zone to use leave blank to run locally"))
parser.add_argument('--placement', action='store_true',
                    help=("whether to launch instances inside a placement "
                          "group"))
parser.add_argument('--gpu', action='store_true',
                    help=("place data on GPU"))
parser.add_argument('--backend', type=str, default='tcp',
                    help='which PyTorch communication backend to use')
parser.add_argument('--name', type=str, default='allreduce',
                     help="name of the current run")
parser.add_argument('--instance-type', type=str, default='t2.large',
                     help="type of instance to use")
parser.add_argument('--data-size-mb', type=int, default=100,
                    help='size of parameters to synchronize, in megabytes')
parser.add_argument('--ami', type=str, default='',
                     help="name of AMI to use ")
parser.add_argument('--num-machines', type=int, default=2,
                     help="size of MPI world")
parser.add_argument('--num-iters', type=int, default=1000,
                     help="how many iterations to run for")
parser.add_argument('--linux-type', type=str, default='ubuntu',
                     help="either 'ubuntu' or 'amazon' for linux type")

# mpi flags
parser.add_argument('--role', type=str, default='launcher',
                    help='launcher or worker, internal flag')
parser.add_argument('--rank', type=int, default=0,
                    help='mpi rank')
parser.add_argument('--master-addr', type=str, default='127.0.0.1',
                    help='address of master node')
parser.add_argument('--master-port', type=int, default=6006,
                    help='port of master node')

args = parser.parse_args()

ami_dict_ubuntu = {
  "us-east-1": "ami-6d720012",
  "us-east-2": "ami-23c4fb46",
  "us-west-2": "ami-e580c79d",
}
ami_dict_amazon = {
  "us-east-1": "ami-ca4464b5",
  "us-east-2": "ami-16f8c073",
  "us-west-2": "ami-f275218a",
}

if args.linux_type == 'ubuntu':
  ami_dict = ami_dict_ubuntu
elif args.linux_type == 'amazon':
  ami_dict = ami_dict_amazon
else:
  assert False, 'unknown linux type '+args.linux_type

def worker():
  """Main body for each worker in MPI world."""
  
  print("Initializing distributed pytorch")
  os.environ['MASTER_ADDR'] = str(args.master_addr)
  os.environ['MASTER_PORT'] = str(args.master_port)
  dist.init_process_group(args.backend, rank=args.rank,
                          world_size=args.num_machines)
  group = dist.new_group(range(args.num_machines))

  params = torch.zeros(args.data_size_mb*250*1000)
  for i in range(args.num_iters):
    grads = torch.ones(args.data_size_mb*250*1000)
    start_time = time.perf_counter()
    dist.all_reduce(grads, op=dist.reduce_op.SUM, group=group)
    elapsed_time = time.perf_counter() - start_time
    rate = args.data_size_mb/elapsed_time

    print("Process %d transferred %d MB in %.1f ms (%.1f MB/sec)" % (args.rank, args.data_size_mb, elapsed_time*1000, rate))

    params+=grads
    #    print('Rank ', args.rank, ' has data ', params[0])


def launcher():
  module_path=os.path.dirname(os.path.abspath(__file__))
  sys.path.append(module_path+'/..')  # aws_backend.py is one level up
  import tmux_backend
  import aws_backend
  import create_resources as create_resources_lib
  import util as u

  if args.placement:
    placement_group = args.name
  else:
    placement_group = ''

  if not args.zone:
    backend = tmux_backend
    run = backend.make_run(args.name)
  else:
    region = u.get_region()
    print("Using region", region)
    assert args.zone.startswith(region), "Availability zone %s must be in default region %s. Default region is taken from environment variable AWS_DEFAULT_REGION" %(args.zone, region)

    if args.ami:
      print("Warning, using provided AMI, make sure that --linux-type argument "
            "is set correctly")
      ami = args.ami
    else:
      assert region in ami_dict, "Define proper AMI mapping for this region."
      ami = ami_dict[region]


    create_resources_lib.create_resources()
    region = u.get_region()
    backend = aws_backend  
    run = backend.make_run(args.name, ami=ami, availability_zone=args.zone,
                           linux_type=args.linux_type)
    
  job = run.make_job('worker', instance_type=args.instance_type,
                     num_tasks=args.num_machines,
                     placement_group=placement_group)
  job.wait_until_ready()

  print("Job ready for connection, to connect to most recent task, run the following:")
  print("../connect "+args.name)
  print("Alternatively run")
  print(job.connect_instructions)
  print()
  print()
  print()
  print()

  print("Task internal IPs")
  for task in job.tasks:
    print(task.ip)
  
  job.upload(__file__)
  if args.zone:
    job.run('killall python || echo failed')  # kill previous run
    job.run('source activate pytorch_p36')

  script_name = os.path.basename(__file__)
  for worker_idx in range(args.num_machines):
    cmd = 'python %s --role=worker --rank=%d --data-size-mb=%d --num-machines=%d --master-addr=%s' %(script_name, worker_idx, args.data_size_mb, args.num_machines, job.tasks[0].ip)
    job.tasks[worker_idx].run(cmd, sync=False)


def main():
  if args.role == "launcher":
    launcher()
  elif args.role == "worker":
    worker()
  else:
    assert False, "Unknown role "+FLAGS.role

  
if __name__ == "__main__":
  main()
