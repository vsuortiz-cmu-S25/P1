import boto3
import botocore
import requests
import time
import json
import re
from datetime import datetime
from dateutil.parser import parse

########################################
# Constants
########################################
with open('auto-scaling-config.json') as file:
    configuration = json.load(file)

LOAD_GENERATOR_AMI = configuration['load_generator_ami']
WEB_SERVICE_AMI = configuration['web_service_ami']
INSTANCE_TYPE = configuration['instance_type']

# Auto Scaling parameters
ASG_MAX_SIZE = configuration['asg_max_size']
ASG_MIN_SIZE = configuration['asg_min_size']
HEALTH_CHECK_GRACE_PERIOD = configuration['health_check_grace_period']
COOL_DOWN_PERIOD_SCALE_IN = configuration['cool_down_period_scale_in']
COOL_DOWN_PERIOD_SCALE_OUT = configuration['cool_down_period_scale_out']
SCALE_OUT_ADJUSTMENT = configuration['scale_out_adjustment']
SCALE_IN_ADJUSTMENT = configuration['scale_in_adjustment']
ASG_DEFAULT_COOL_DOWN_PERIOD = configuration['asg_default_cool_down_period']
ALARM_PERIOD = configuration['alarm_period']
CPU_LOWER_THRESHOLD = configuration['cpu_lower_threshold']
CPU_UPPER_THRESHOLD = configuration['cpu_upper_threshold']
ALARM_EVALUATION_PERIODS_SCALE_OUT = configuration['alarm_evaluation_periods_scale_out']
ALARM_EVALUATION_PERIODS_SCALE_IN = configuration['alarm_evaluation_periods_scale_in']
AUTO_SCALING_TARGET_GROUP = configuration['auto_scaling_target_group']
LOAD_BALANCER_NAME = configuration['load_balancer_name']
LAUNCH_TEMPLATE_NAME = configuration['launch_template_name']
AUTO_SCALING_GROUP_NAME = configuration['auto_scaling_group_name']

########################################
# Tags
########################################
tag_pairs = [
    ("Project", "vm-scaling"),
]
TAGS = [{'Key': k, 'Value': v} for k, v in tag_pairs]

TEST_NAME_REGEX = r'name=(.*log)'

# Global resources for cleanup
resources = {
    'lg_instance_id': None,
    'asg_name': None,
    'lt_name': None,
    'lt_id': None,
    'lb_arn': None,
    'tg_arn': None,
    'sg1_id': None,
    'sg2_id': None,
    'scale_out_policy_arn': None,
    'scale_in_policy_arn': None,
    'scale_out_alarm_name': None,
    'scale_in_alarm_name': None
}

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
    
    # Get default subnet for availability zone us-east-1a for consistency
    ec2_client = boto3.client('ec2', region_name='us-east-1')
    subnets = ec2_client.describe_subnets(
        Filters=[
            {'Name': 'default-for-az', 'Values': ['true']},
            {'Name': 'availability-zone', 'Values': ['us-east-1a']}
        ]
    )
    
    if not subnets['Subnets']:
        # Fallback to any default subnet
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


def initialize_test(load_generator_dns, first_web_service_dns):
    """
    Start the auto scaling test
    :param lg_dns: Load Generator DNS
    :param first_web_service_dns: Web service DNS
    :return: Log file name
    """

    add_ws_string = 'http://{}/autoscaling?dns={}'.format(
        load_generator_dns, first_web_service_dns
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


def initialize_warmup(load_generator_dns, load_balancer_dns):
    """
    Start the warmup test
    :param lg_dns: Load Generator DNS
    :param load_balancer_dns: Load Balancer DNS
    :return: Log file name
    """

    add_ws_string = 'http://{}/warmup?dns={}'.format(
        load_generator_dns, load_balancer_dns
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


def get_test_id(response):
    """
    Extracts the test id from the server response.
    :param response: the server response.
    :return: the test name (log file name).
    """
    response_text = response.text

    regexpr = re.compile(TEST_NAME_REGEX)

    return regexpr.findall(response_text)[0]


def destroy_resources():
    """
    Delete all resources created for this task

    You must destroy the following resources:
    Load Generator, Auto Scaling Group, Launch Template, Load Balancer, Security Group.
    Note that one resource may depend on another, and if resource A depends on resource B, you must delete resource B before you can delete resource A.
    Below are all the resource dependencies that you need to consider in order to decide the correct ordering of resource deletion.

    - You cannot delete Launch Template before deleting the Auto Scaling Group
    - You cannot delete a Security group before deleting the Load Generator and the Auto Scaling Groups
    - You must wait for the instances in your target group to be terminated before deleting the security groups

    :param msg: message
    :return: None
    """
    print_section('Destroying Resources')
    
    ec2_client = boto3.client('ec2', region_name='us-east-1')
    elb_client = boto3.client('elbv2', region_name='us-east-1')
    asg_client = boto3.client('autoscaling', region_name='us-east-1')
    cw_client = boto3.client('cloudwatch', region_name='us-east-1')
    
    try:
        # Step 1: Delete CloudWatch alarms
        print("Step 1: Deleting CloudWatch alarms...")
        if resources['scale_out_alarm_name']:
            try:
                cw_client.delete_alarms(AlarmNames=[resources['scale_out_alarm_name']])
                print(f"  Deleted CloudWatch alarm: {resources['scale_out_alarm_name']}")
            except Exception as e:
                print(f"  Error deleting scale out alarm: {e}")
        
        if resources['scale_in_alarm_name']:
            try:
                cw_client.delete_alarms(AlarmNames=[resources['scale_in_alarm_name']])
                print(f"  Deleted CloudWatch alarm: {resources['scale_in_alarm_name']}")
            except Exception as e:
                print(f"  Error deleting scale in alarm: {e}")
        
        # Step 2: Update ASG to 0 instances and wait for termination
        if resources['asg_name']:
            print("Step 2: Updating ASG to 0 instances...")
            try:
                # Update ASG to have 0 instances
                asg_client.update_auto_scaling_group(
                    AutoScalingGroupName=resources['asg_name'],
                    MinSize=0,
                    MaxSize=0,
                    DesiredCapacity=0
                )
                
                # Step 3: Delete ASG
                print("Step 3: Deleting Auto Scaling Group...")
                asg_client.delete_auto_scaling_group(
                    AutoScalingGroupName=resources['asg_name'],
                    ForceDelete=True
                )
                print(f"  Deleted Auto Scaling Group: {resources['asg_name']}")
                
            except Exception as e:
                print(f"  Error with ASG operations: {e}")
        
        # Step 4: Delete Load Balancer listeners
        if resources['lb_arn']:
            print("Step 4: Deleting Load Balancer listeners...")
            try:
                listeners = elb_client.describe_listeners(LoadBalancerArn=resources['lb_arn'])
                for listener in listeners['Listeners']:
                    elb_client.delete_listener(ListenerArn=listener['ListenerArn'])
                    print(f"  Deleted listener: {listener['ListenerArn']}")
            except Exception as e:
                print(f"  Error deleting listeners: {e}")
            
            # Step 5: Delete Load Balancer and wait for full deletion
            print("Step 5: Deleting Load Balancer...")
            try:
                elb_client.delete_load_balancer(LoadBalancerArn=resources['lb_arn'])
                print(f"  Initiated deletion of Load Balancer")
                
                # Wait for Load Balancer to be fully deleted
                print("  Waiting for Load Balancer to be fully deleted...")
                waiter = elb_client.get_waiter('load_balancers_deleted')
                waiter.wait(
                    LoadBalancerArns=[resources['lb_arn']],
                    WaiterConfig={'Delay': 15, 'MaxAttempts': 40}
                )
                print("  Load Balancer fully deleted")
            except Exception as e:
                print(f"  Error deleting Load Balancer: {e}")
        
        # Step 6: Wait for Target Group targets to deregister
        if resources['tg_arn']:
            print("Step 6: Waiting for Target Group targets to deregister...")
            try:
                max_wait_time = 120  # 2 minutes
                start_time = time.time()
                
                while time.time() - start_time < max_wait_time:
                    response = elb_client.describe_target_health(TargetGroupArn=resources['tg_arn'])
                    targets = response.get('TargetHealthDescriptions', [])
                    if not targets:
                        print("  All targets deregistered")
                        break
                    print(f"    Still waiting... {len(targets)} targets remaining")
                    time.sleep(10)
                else:
                    print("  Warning: Timeout waiting for targets to deregister")
                
                # Step 7: Delete Target Group
                print("Step 7: Deleting Target Group...")
                elb_client.delete_target_group(TargetGroupArn=resources['tg_arn'])
                print(f"  Deleted Target Group")
            except Exception as e:
                print(f"  Error with Target Group operations: {e}")
        
        # Step 8: Delete Launch Template
        if resources['lt_id']:
            print("Step 8: Deleting Launch Template...")
            try:
                ec2_client.delete_launch_template(LaunchTemplateId=resources['lt_id'])
                print(f"  Deleted Launch Template: {resources['lt_name']}")
            except Exception as e:
                print(f"  Error deleting Launch Template: {e}")
        
        # Step 9: Terminate Load Generator
        if resources['lg_instance_id']:
            print("Step 9: Terminating Load Generator instance...")
            try:
                ec2_client.terminate_instances(InstanceIds=[resources['lg_instance_id']])
                print(f"  Initiated termination of Load Generator: {resources['lg_instance_id']}")
                
                # Wait for instance to terminate
                waiter = ec2_client.get_waiter('instance_terminated')
                waiter.wait(
                    InstanceIds=[resources['lg_instance_id']],
                    WaiterConfig={'Delay': 15, 'MaxAttempts': 40}
                )
                print("  Load Generator terminated")
            except Exception as e:
                print(f"  Error terminating Load Generator: {e}")
        
        # sg1_id is for Load Generator (non-LB)
        # sg2_id is for ASG and ELB (LB-associated)
        print(f"  Non-LB security group (Load Generator): {resources['sg1_id']}")
        print(f"  LB-associated security group (ASG/ELB): {resources['sg2_id']}")
        
        # Wait a bit for resources to fully clean up
        print("Waiting for network interfaces to detach...")
        time.sleep(30)
        
        # Step 11: Delete non-LB security group first (sg1 - Load Generator)
        if resources['sg1_id']:
            print("Step 11: Deleting non-LB security group (Load Generator)...")
            try:
                ec2_client.delete_security_group(GroupId=resources['sg1_id'])
                print(f"  Deleted Load Generator Security Group: {resources['sg1_id']}")
            except botocore.exceptions.ClientError as e:
                print(f"  Error deleting Load Generator Security Group: {e}")
        
        # Step 12: Delete LB-associated security group last (sg2 - ASG/ELB)
        if resources['sg2_id']:
            print("Step 13: Deleting LB-associated security group (ASG/ELB)...")
            try:
                ec2_client.delete_security_group(GroupId=resources['sg2_id'])
                print(f"  Deleted ASG/ELB Security Group: {resources['sg2_id']}")
            except botocore.exceptions.ClientError as e:
                print(f"  Error deleting ASG/ELB Security Group: {e}")
        
        print("\nResource cleanup completed successfully!")
        
    except Exception as e:
        print(f"Unexpected error during resource cleanup: {e}")
        import traceback
        traceback.print_exc()


def print_section(msg):
    """
    Print a section separator including given message
    :param msg: message
    :return: None
    """
    print(('#' * 40) + '\n# ' + msg + '\n' + ('#' * 40))


def is_test_complete(load_generator_dns, log_name):
    """
    Check if auto scaling test is complete
    :param load_generator_dns: lg dns
    :param log_name: log file name
    :return: True if Auto Scaling test is complete and False otherwise.
    """
    log_string = 'http://{}/log?name={}'.format(load_generator_dns, log_name)

    # creates a log file for submission and monitoring
    f = open(log_name + ".log", "w")
    log_text = requests.get(log_string).text
    f.write(log_text)
    f.close()

    return '[Test finished]' in log_text


########################################
# Main routine
########################################
def main():
    # BIG PICTURE TODO: Programmatically provision autoscaling resources
    #   - Create security groups for Load Generator and ASG, ELB
    #   - Provision a Load Generator
    #   - Generate a Launch Template
    #   - Create a Target Group
    #   - Provision a Load Balancer
    #   - Associate Target Group with Load Balancer
    #   - Create an Autoscaling Group
    #   - Initialize Warmup Test
    #   - Initialize Autoscaling Test
    #   - Terminate Resources

    print_section('1 - create two security groups')

    PERMISSIONS = [
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
    
    # Create security groups
    def create_security_group(name, description):
        try:
            response = ec2_client.create_security_group(
                GroupName=name,
                Description=description,
                VpcId=vpc_id,
                TagSpecifications=[{'ResourceType': 'security-group', 'Tags': TAGS}]
            )
            sg_id = response['GroupId']
            # Add HTTP ingress rule
            ec2_client.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=PERMISSIONS
            )
            print(f"Created {name}: {sg_id}")
            return sg_id
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'InvalidGroup.Duplicate':
                sgs = ec2_client.describe_security_groups(
                    Filters=[{'Name': 'group-name', 'Values': [name]}]
                )
                sg_id = sgs['SecurityGroups'][0]['GroupId']
                print(f"Using existing {name}: {sg_id}")
                return sg_id
            raise e
    
    sg1_id = create_security_group('AutoScalingLGSecGroup', 'Load Generator security group')
    sg2_id = create_security_group('AutoScalingASGSecGroup', 'ASG and ELB security group')
    
    resources['sg1_id'] = sg1_id
    resources['sg2_id'] = sg2_id

    print_section('2 - create LG')

    # Create Load Generator instance
    lg = create_instance(LOAD_GENERATOR_AMI, sg1_id)
    lg_id = lg.instance_id
    lg_dns = lg.public_dns_name
    resources['lg_instance_id'] = lg_id
    print("Load Generator running: id={} dns={}".format(lg_id, lg_dns))

    print_section('3. Create LT (Launch Template)')
    
    # Create Launch Template
    lt_response = ec2_client.create_launch_template(
        LaunchTemplateName=LAUNCH_TEMPLATE_NAME,
        LaunchTemplateData={
            'ImageId': WEB_SERVICE_AMI,
            'InstanceType': INSTANCE_TYPE,
            'SecurityGroupIds': [sg2_id],
            'Monitoring': {
                'Enabled': True
            },
            'TagSpecifications': [
                {
                    'ResourceType': 'instance',
                    'Tags': TAGS
                },
                {
                    'ResourceType': 'volume',
                    'Tags': TAGS
                }
            ]
        },
        TagSpecifications=[
            {
                'ResourceType': 'launch-template',
                'Tags': TAGS
            }
        ]
    )
    lt_id = lt_response['LaunchTemplate']['LaunchTemplateId']
    lt_name = lt_response['LaunchTemplate']['LaunchTemplateName']
    resources['lt_id'] = lt_id
    resources['lt_name'] = lt_name
    print(f"Created Launch Template: {lt_name} (ID: {lt_id})")

    print_section('4. Create TG (Target Group)')
    
    # Create Target Group
    elb_client = boto3.client('elbv2', region_name='us-east-1')
    
    tg_response = elb_client.create_target_group(
        Name=AUTO_SCALING_TARGET_GROUP,
        Protocol='HTTP',
        Port=80,
        VpcId=vpc_id,
        HealthCheckEnabled=True,
        HealthCheckProtocol='HTTP',
        HealthCheckPort='80',
        HealthCheckPath='/',
        HealthCheckIntervalSeconds=30,
        HealthCheckTimeoutSeconds=5,
        HealthyThresholdCount=2,
        UnhealthyThresholdCount=3,
        TargetType='instance',
        Tags=TAGS
    )
    tg_arn = tg_response['TargetGroups'][0]['TargetGroupArn']
    resources['tg_arn'] = tg_arn
    print(f"Created Target Group: {AUTO_SCALING_TARGET_GROUP} (ARN: {tg_arn})")

    print_section('5. Create ELB (Elastic/Application Load Balancer)')

    # Get subnets for the Load Balancer (need at least 2 availability zones)
    subnets_response = ec2_client.describe_subnets(
        Filters=[
            {'Name': 'vpc-id', 'Values': [vpc_id]},
            {'Name': 'default-for-az', 'Values': ['true']}
        ]
    )
    subnet_ids = [subnet['SubnetId'] for subnet in subnets_response['Subnets']]
    # Use at least 2 subnets for ALB
    subnet_ids = subnet_ids[:2] if len(subnet_ids) >= 2 else subnet_ids

    # Create Application Load Balancer
    lb_response = elb_client.create_load_balancer(
        Name=LOAD_BALANCER_NAME,
        Subnets=subnet_ids,
        SecurityGroups=[sg2_id],
        Scheme='internet-facing',
        Tags=TAGS,
        Type='application',
        IpAddressType='ipv4'
    )
    lb_arn = lb_response['LoadBalancers'][0]['LoadBalancerArn']
    lb_dns = lb_response['LoadBalancers'][0]['DNSName']
    resources['lb_arn'] = lb_arn
    print("lb started. ARN={}, DNS={}".format(lb_arn, lb_dns))

    print_section('6. Associate ELB with target group')
    
    # Create listener to associate ELB with Target Group
    listener_response = elb_client.create_listener(
        LoadBalancerArn=lb_arn,
        Protocol='HTTP',
        Port=80,
        DefaultActions=[
            {
                'Type': 'forward',
                'TargetGroupArn': tg_arn
            }
        ]
    )
    print(f"Created listener to forward traffic from ELB to Target Group")

    print_section('7. Create ASG (Auto Scaling Group)')
    
    # Create Auto Scaling Group
    asg_client = boto3.client('autoscaling', region_name='us-east-1')
    
    # Use the first subnet for ASG (single AZ for better performance)
    asg_subnet = subnets_response['Subnets'][0]['SubnetId']
    
    asg_response = asg_client.create_auto_scaling_group(
        AutoScalingGroupName=AUTO_SCALING_GROUP_NAME,
        LaunchTemplate={
            'LaunchTemplateId': lt_id,
            'Version': '$Latest'
        },
        MinSize=ASG_MIN_SIZE,
        MaxSize=ASG_MAX_SIZE,
        DesiredCapacity=ASG_MIN_SIZE,
        DefaultCooldown=ASG_DEFAULT_COOL_DOWN_PERIOD,
        HealthCheckType='EC2',
        HealthCheckGracePeriod=HEALTH_CHECK_GRACE_PERIOD,
        VPCZoneIdentifier=asg_subnet,
        TargetGroupARNs=[tg_arn],
        Tags=[
            {
                'Key': tag['Key'],
                'Value': tag['Value'],
                'PropagateAtLaunch': True,
                'ResourceId': AUTO_SCALING_GROUP_NAME,
                'ResourceType': 'auto-scaling-group'
            } for tag in TAGS
        ]
    )
    resources['asg_name'] = AUTO_SCALING_GROUP_NAME
    print(f"Created Auto Scaling Group: {AUTO_SCALING_GROUP_NAME}")
    
    # Enable metrics collection for ASG
    asg_client.enable_metrics_collection(
        AutoScalingGroupName=AUTO_SCALING_GROUP_NAME,
        Metrics=[
            'GroupMinSize',
            'GroupMaxSize',
            'GroupDesiredCapacity',
            'GroupInServiceInstances',
            'GroupTotalInstances'
        ],
        Granularity='1Minute'
    )

    print_section('8. Create policy and attached to ASG')
    
    # Create Scale Out Policy
    scale_out_policy = asg_client.put_scaling_policy(
        AutoScalingGroupName=AUTO_SCALING_GROUP_NAME,
        PolicyName=f'{AUTO_SCALING_GROUP_NAME}-scale-out',
        PolicyType='SimpleScaling',
        AdjustmentType='ChangeInCapacity',
        ScalingAdjustment=SCALE_OUT_ADJUSTMENT,
        Cooldown=COOL_DOWN_PERIOD_SCALE_OUT
    )
    scale_out_policy_arn = scale_out_policy['PolicyARN']
    resources['scale_out_policy_arn'] = scale_out_policy_arn
    print(f"Created Scale Out Policy (adds {SCALE_OUT_ADJUSTMENT} instances)")
    
    # Create Scale In Policy
    scale_in_policy = asg_client.put_scaling_policy(
        AutoScalingGroupName=AUTO_SCALING_GROUP_NAME,
        PolicyName=f'{AUTO_SCALING_GROUP_NAME}-scale-in',
        PolicyType='SimpleScaling',
        AdjustmentType='ChangeInCapacity',
        ScalingAdjustment=SCALE_IN_ADJUSTMENT,
        Cooldown=COOL_DOWN_PERIOD_SCALE_IN
    )
    scale_in_policy_arn = scale_in_policy['PolicyARN']
    resources['scale_in_policy_arn'] = scale_in_policy_arn
    print(f"Created Scale In Policy (removes {abs(SCALE_IN_ADJUSTMENT)} instances)")

    print_section('9. Create Cloud Watch alarm. Action is to invoke policy.')
    
    # Create CloudWatch client
    cw_client = boto3.client('cloudwatch', region_name='us-east-1')
    
    # Create Scale Out Alarm (high CPU)
    scale_out_alarm_name = f'{AUTO_SCALING_GROUP_NAME}-scale-out-alarm'
    cw_client.put_metric_alarm(
        AlarmName=scale_out_alarm_name,
        ComparisonOperator='GreaterThanThreshold',
        EvaluationPeriods=ALARM_EVALUATION_PERIODS_SCALE_OUT,
        MetricName='CPUUtilization',
        Namespace='AWS/EC2',
        Period=ALARM_PERIOD,
        Statistic='Average',
        Threshold=CPU_UPPER_THRESHOLD,
        ActionsEnabled=True,
        AlarmActions=[scale_out_policy_arn],
        AlarmDescription=f'Trigger scale out when CPU > {CPU_UPPER_THRESHOLD}%',
        Dimensions=[
            {
                'Name': 'AutoScalingGroupName',
                'Value': AUTO_SCALING_GROUP_NAME
            }
        ],
        Unit='Percent'
    )
    resources['scale_out_alarm_name'] = scale_out_alarm_name
    print(f"Created Scale Out Alarm (CPU > {CPU_UPPER_THRESHOLD}%)")
    
    # Create Scale In Alarm (low CPU)
    scale_in_alarm_name = f'{AUTO_SCALING_GROUP_NAME}-scale-in-alarm'
    cw_client.put_metric_alarm(
        AlarmName=scale_in_alarm_name,
        ComparisonOperator='LessThanThreshold',
        EvaluationPeriods=ALARM_EVALUATION_PERIODS_SCALE_IN,
        MetricName='CPUUtilization',
        Namespace='AWS/EC2',
        Period=ALARM_PERIOD,
        Statistic='Average',
        Threshold=CPU_LOWER_THRESHOLD,
        ActionsEnabled=True,
        AlarmActions=[scale_in_policy_arn],
        AlarmDescription=f'Trigger scale in when CPU < {CPU_LOWER_THRESHOLD}%',
        Dimensions=[
            {
                'Name': 'AutoScalingGroupName',
                'Value': AUTO_SCALING_GROUP_NAME
            }
        ],
        Unit='Percent'
    )
    resources['scale_in_alarm_name'] = scale_in_alarm_name
    print(f"Created Scale In Alarm (CPU < {CPU_LOWER_THRESHOLD}%)")
    
    # Wait for Load Balancer to be active
    print("Waiting for Load Balancer to be active...")
    waiter = elb_client.get_waiter('load_balancer_available')
    waiter.wait(LoadBalancerArns=[lb_arn])
    
    # Wait for at least one instance to be healthy in target group
    print("Waiting for instances to be healthy in target group...")
    time.sleep(60)  # Give time for instances to launch and become healthy

    print_section('10. Submit ELB DNS to LG, starting warm up test.')
    warmup_log_name = initialize_warmup(lg_dns, lb_dns)
    while not is_test_complete(lg_dns, warmup_log_name):
        time.sleep(1)

    print_section('11. Submit ELB DNS to LG, starting auto scaling test.')
    # May take a few minutes to start actual test after warm up test finishes
    log_name = initialize_test(lg_dns, lb_dns)
    while not is_test_complete(lg_dns, log_name):
        time.sleep(1)

    destroy_resources()


if __name__ == "__main__":
    main()