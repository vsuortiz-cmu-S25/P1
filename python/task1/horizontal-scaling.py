
import boto3
import botocore
import requests
import time
import json
import configparser
import re
from datetime import datetime
from dateutil.parser import parse


########################################
# Constants
########################################
with open('horizontal-scaling-config.json') as file:
    configuration = json.load(file)

LOAD_GENERATOR_AMI = configuration['load_generator_ami']
WEB_SERVICE_AMI = configuration['web_service_ami']
INSTANCE_TYPE = configuration['instance_type']

########################################
# Tags
########################################
tag_pairs = [
    ("Project", "vm-scaling"),
]
TAGS = [{'Key': k, 'Value': v} for k, v in tag_pairs]

TEST_NAME_REGEX = r'name=(.*log)'

########################################
# Utility functions
########################################


def create_instance(ami, sg_id):
    """
    Given AMI, create and return an AWS EC2 instance object
    :param ami: AMI image name to launch the instance with
    :param sg_id: ID of the security group to be attached to instance
    :return: instance object
    """
    ec2 = boto3.resource('ec2', region_name='us-east-1')
    
    # Get default subnet for availability zone
    ec2_client = boto3.client('ec2', region_name='us-east-1')
    subnets = ec2_client.describe_subnets(
        Filters=[{'Name': 'default-for-az', 'Values': ['true']}]
    )
    subnet_id = subnets['Subnets'][0]['SubnetId']
    
    # Launch instance
    instances = ec2.create_instances(
        ImageId=ami,
        InstanceType=INSTANCE_TYPE,
        SecurityGroupIds=[sg_id],
        SubnetId=subnet_id,
        MaxCount=1,
        MinCount=1,
        TagSpecifications=[
            {
                'ResourceType': 'instance',
                'Tags': TAGS
            },
            {
                'ResourceType': 'volume',
                'Tags': TAGS
            },
            {
                'ResourceType': 'network-interface',
                'Tags': TAGS
            }
        ]
    )
    instance = instances[0]
    
    # Wait for instance to be running
    instance.wait_until_running()
    instance.reload()
    
    return instance


def initialize_test(lg_dns, first_web_service_dns):
    """
    Start the horizontal scaling test
    :param lg_dns: Load Generator DNS
    :param first_web_service_dns: Web service DNS
    :return: Log file name
    """

    add_ws_string = 'http://{}/test/horizontal?dns={}'.format(
        lg_dns, first_web_service_dns
    )
    response = None
    while not response or response.status_code != 200:
        try:
            response = requests.get(add_ws_string)
        except requests.exceptions.ConnectionError:
            time.sleep(1)
            pass 

    # Extract test log name from response
    log_name = get_test_id(response)
    return log_name


def print_section(msg):
    """
    Print a section separator including given message
    :param msg: message
    :return: None
    """
    print(('#' * 40) + '\n# ' + msg + '\n' + ('#' * 40))


def get_test_id(response):
    """
    Extracts the test id from the server response.
    :param response: the server response.
    :return: the test name (log file name).
    """
    response_text = response.text

    regexpr = re.compile(TEST_NAME_REGEX)

    return regexpr.findall(response_text)[0]


def is_test_complete(lg_dns, log_name):
    """
    Check if the horizontal scaling test has finished
    :param lg_dns: load generator DNS
    :param log_name: name of the log file
    :return: True if Horizontal Scaling test is complete and False otherwise.
    """

    log_string = 'http://{}/log?name={}'.format(lg_dns, log_name)

    # creates a log file for submission and monitoring
    f = open(log_name + ".log", "w")
    log_text = requests.get(log_string).text
    f.write(log_text)
    f.close()

    return '[Test finished]' in log_text


def add_web_service_instance(lg_dns, sg2_id, log_name):
    """
    Launch a new WS (Web Server) instance and add to the test
    :param lg_dns: load generator DNS
    :param sg2_id: id of WS security group
    :param log_name: name of the log file
    """
    ins = create_instance(WEB_SERVICE_AMI, sg2_id)
    print("New WS launched. id={}, dns={}".format(
        ins.instance_id,
        ins.public_dns_name)
    )
    add_req = 'http://{}/test/horizontal/add?dns={}'.format(
        lg_dns,
        ins.public_dns_name
    )
    while True:
        if requests.get(add_req).status_code == 200:
            print("New WS submitted to LG.")
            break
        elif is_test_complete(lg_dns, log_name):
            print("New WS not submitted because test already completed.")
            break


def get_rps(lg_dns, log_name):
    """
    Return the current RPS as a floating point number
    :param lg_dns: LG DNS
    :param log_name: name of log file
    :return: latest RPS value
    """

    log_string = 'http://{}/log?name={}'.format(lg_dns, log_name)
    config = configparser.ConfigParser(strict=False)
    config.read_string(requests.get(log_string).text)
    sections = config.sections()
    sections.reverse()
    rps = 0
    for sec in sections:
        if 'Current rps=' in sec:
            rps = float(sec[len('Current rps='):])
            break
    return rps


def get_test_start_time(lg_dns, log_name):
    """
    Return the test start time in UTC
    :param lg_dns: LG DNS
    :param log_name: name of log file
    :return: datetime object of the start time in UTC
    """
    log_string = 'http://{}/log?name={}'.format(lg_dns, log_name)
    start_time = None
    while start_time is None:
        config = configparser.ConfigParser(strict=False)
        config.read_string(requests.get(log_string).text)
        # By default, options names in a section are converted
        # to lower case by configparser
        start_time = dict(config.items('Test')).get('starttime', None)
    return parse(start_time)


########################################
# Main routine
########################################
def main():
    # BIG PICTURE TODO: Provision resources to achieve horizontal scalability
    #   - Create security groups for Load Generator and Web Service
    #   - Provision a Load Generator instance
    #   - Provision a Web Service instance
    #   - Register Web Service DNS with Load Generator
    #   - Add Web Service instances to Load Generator
    #   - Terminate resources

    print_section('1 - create two security groups')
    sg_permissions = [
        {'IpProtocol': 'tcp',
         'FromPort': 80,
         'ToPort': 80,
         'IpRanges': [{'CidrIp': '0.0.0.0/0'}],
         'Ipv6Ranges': [{'CidrIpv6': '::/0'}],
         }
    ]

    # Create EC2 client
    ec2_client = boto3.client('ec2', region_name='us-east-1')
    
    # Get the default VPC
    vpcs = ec2_client.describe_vpcs(Filters=[{'Name': 'is-default', 'Values': ['true']}])
    vpc_id = vpcs['Vpcs'][0]['VpcId']
    print(f"Using default VPC: {vpc_id}")
    
    # Create security group for Load Generator
    lg_sg_name = 'LGSecGroup'
    try:
        # Try to create the security group
        lg_sg_response = ec2_client.create_security_group(
            GroupName=lg_sg_name,
            Description='Security group for Load Generator instances',
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    'ResourceType': 'security-group',
                    'Tags': TAGS
                }
            ]
        )
        sg1_id = lg_sg_response['GroupId']
        print(f"Created Load Generator security group: {sg1_id}")
        
        # Add ingress rules for Load Generator security group
        ec2_client.authorize_security_group_ingress(
            GroupId=sg1_id,
            IpPermissions=sg_permissions
        )
        print(f"Added HTTP ingress rule to Load Generator security group")
        
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.Duplicate':
            # Security group already exists, get its ID
            sgs = ec2_client.describe_security_groups(
                Filters=[
                    {'Name': 'group-name', 'Values': [lg_sg_name]},
                    {'Name': 'vpc-id', 'Values': [vpc_id]}
                ]
            )
            sg1_id = sgs['SecurityGroups'][0]['GroupId']
            print(f"Using existing Load Generator security group: {sg1_id}")
        else:
            raise e
    
    # Create security group for Web Service
    ws_sg_name = 'WSSecGroup'
    try:
        # Try to create the security group
        ws_sg_response = ec2_client.create_security_group(
            GroupName=ws_sg_name,
            Description='Security group for Web Service instances',
            VpcId=vpc_id,
            TagSpecifications=[
                {
                    'ResourceType': 'security-group',
                    'Tags': TAGS
                }
            ]
        )
        sg2_id = ws_sg_response['GroupId']
        print(f"Created Web Service security group: {sg2_id}")
        
        # Add ingress rules for Web Service security group
        ec2_client.authorize_security_group_ingress(
            GroupId=sg2_id,
            IpPermissions=sg_permissions
        )
        print(f"Added HTTP ingress rule to Web Service security group")
        
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'InvalidGroup.Duplicate':
            # Security group already exists, get its ID
            sgs = ec2_client.describe_security_groups(
                Filters=[
                    {'Name': 'group-name', 'Values': [ws_sg_name]},
                    {'Name': 'vpc-id', 'Values': [vpc_id]}
                ]
            )
            sg2_id = sgs['SecurityGroups'][0]['GroupId']
            print(f"Using existing Web Service security group: {sg2_id}")
        else:
            raise e

    print_section('2 - create LG')

    # Create Load Generator instance
    lg = create_instance(LOAD_GENERATOR_AMI, sg1_id)
    lg_id = lg.instance_id
    lg_dns = lg.public_dns_name
    print("Load Generator running: id={} dns={}".format(lg_id, lg_dns))

    print_section('3 - create first WS')
    
    # Create First Web Service Instance
    ws = create_instance(WEB_SERVICE_AMI, sg2_id)
    web_service_dns = ws.public_dns_name
    print("First Web Service running: id={} dns={}".format(ws.instance_id, web_service_dns))

    print_section('3. Submit the first WS instance DNS to LG, starting test.')
    log_name = initialize_test(lg_dns, web_service_dns)
    last_launch_time = get_test_start_time(lg_dns, log_name)
    
    # Track all instances for cleanup
    all_instances = [lg, ws]
    
    while not is_test_complete(lg_dns, log_name):
        # Get current RPS
        current_rps = get_rps(lg_dns, log_name)
        current_time = time.time()
        
        # Check if we need to add more instances
        if current_rps < 50:
            # Check if cooldown period has passed (100 seconds)
            time_since_last_launch = current_time - last_launch_time.timestamp()
            if time_since_last_launch >= 100:
                print(f"Current RPS: {current_rps:.2f} < 50. Adding new WS instance...")
                new_ws = create_instance(WEB_SERVICE_AMI, sg2_id)
                all_instances.append(new_ws)
                add_web_service_instance(lg_dns, sg2_id, log_name)
                last_launch_time = datetime.now()
                print(f"New WS added. Total instances: {len(all_instances) - 1} WS + 1 LG")
        
        time.sleep(1)

    print_section('End Test')

    # Terminate all instances
    print("Terminating all instances...")
    ec2 = boto3.resource('ec2', region_name='us-east-1')
    for instance in all_instances:
        instance.terminate()
        print(f"Terminated instance: {instance.instance_id}")
    print("All instances terminated.")


if __name__ == '__main__':
    main()
