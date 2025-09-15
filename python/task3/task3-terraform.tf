###########################################################################
# Template for Task 3 AWS AutoScaling Test                                #
# Do not edit the first section                                           #
# Only edit the second section to configure appropriate scaling policies  #
###########################################################################

############################
# FIRST SECTION BEGINS     #
# DO NOT EDIT THIS SECTION #
############################
locals {
  common_tags = {
    Project = "vm-scaling"
  }
  asg_tags = {
    key                 = "Project"
    value               = "vm-scaling"
    propagate_at_launch = true
  }
}

provider "aws" {
  region = "us-east-1"
}


resource "aws_security_group" "lg" {
  # HTTP access from anywhere
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # outbound internet access
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.common_tags
}

resource "aws_security_group" "elb_asg" {
  # HTTP access from anywhere
  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # outbound internet access
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = local.common_tags
}

######################
# FIRST SECTION ENDS #
######################

############################
# SECOND SECTION BEGINS    #
# PLEASE EDIT THIS SECTION #
############################

# Step 1:
# TODO: Add missing values below
# ================================
resource "aws_launch_template" "lt" {
  name            = "autoscaling-lt"
  image_id        = "ami-0e3d567ccafde16c5"
  instance_type   = "m5.large"

  monitoring {
    enabled = true
  }

  vpc_security_group_ids = [aws_security_group.elb_asg.id]

  tag_specifications {
    resource_type = "instance"
    tags = {
      Project = "vm-scaling"
    }
  }
}

# Create an auto scaling group with appropriate parameters
# TODO: fill the missing values per the placeholders
resource "aws_autoscaling_group" "asg" {
  name                      = "autoscaling-asg"
  availability_zones        = ["us-east-1a"]
  max_size                  = 7
  min_size                  = 1
  desired_capacity          = 1
  default_cooldown          = 60
  health_check_grace_period = 60
  health_check_type         = "EC2"
  launch_template {
    id = aws_launch_template.lt.id
  }
  target_group_arns         = [aws_lb_target_group.tg.arn]
  enabled_metrics           = ["GroupCPUUtilization"]
  tag {
    key = local.asg_tags.key
    value = local.asg_tags.value
    propagate_at_launch = local.asg_tags.propagate_at_launch
  }
}

# TODO: Create a Load Generator AWS instance with proper tags
resource "aws_instance" "lg" {
  ami                    = "ami-0469ff4742c562d63"
  instance_type          = "m5.large"
  vpc_security_group_ids = [aws_security_group.lg.id]

  tags = merge(
    local.common_tags,
    {
      Name = "load-generator"
    }
  )
}

# Step 2:
# TODO: Create an Application Load Balancer with appropriate listeners and target groups
# The lb_listener documentation demonstrates how to connect these resources
# Create and attach your subnet to the Application Load Balancer 
#
# https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb
# https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/subnet
# https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb_listener
# https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lb_target_group

# Use default VPC resource
resource "aws_default_vpc" "default" {
  tags = local.common_tags
}

# Use default subnets
resource "aws_default_subnet" "default_az1" {
  availability_zone = "us-east-1a"
  tags = local.common_tags
}

resource "aws_default_subnet" "default_az2" {
  availability_zone = "us-east-1b"
  tags = local.common_tags
}

# Create Target Group
resource "aws_lb_target_group" "tg" {
  name     = "autoscaling-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = aws_default_vpc.default.id

  health_check {
    enabled             = true
    healthy_threshold   = 2
    unhealthy_threshold = 2
    timeout             = 5
    interval            = 30
    path                = "/"
    matcher             = "200"
  }

  tags = local.common_tags
}

# Create Application Load Balancer
resource "aws_lb" "alb" {
  name               = "autoscaling-lb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.elb_asg.id]
  subnets            = [aws_default_subnet.default_az1.id, aws_default_subnet.default_az2.id]

  enable_deletion_protection = false
  enable_http2              = true

  tags = local.common_tags
}

# Create Load Balancer Listener
resource "aws_lb_listener" "front_end" {
  load_balancer_arn = aws_lb.alb.arn
  port              = "80"
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.tg.arn
  }
}

# Step 3:
# TODO: Create 2 policies: 1 for scaling out and another for scaling in
# Link it to the autoscaling group you created above
# https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/autoscaling_policy

# Scale Out Policy
resource "aws_autoscaling_policy" "scale_out" {
  name                   = "scale-out-policy"
  scaling_adjustment     = 1
  adjustment_type        = "ChangeInCapacity"
  cooldown              = 45
  autoscaling_group_name = aws_autoscaling_group.asg.name
  policy_type           = "SimpleScaling"
}

# Scale In Policy
resource "aws_autoscaling_policy" "scale_in" {
  name                   = "scale-in-policy"
  scaling_adjustment     = -1
  adjustment_type        = "ChangeInCapacity"
  cooldown              = 30
  autoscaling_group_name = aws_autoscaling_group.asg.name
  policy_type           = "SimpleScaling"
}

# Step 4:
# TODO: Create 2 cloudwatch alarms: 1 for scaling out and another for scaling in
# Link it to the autoscaling group you created above
# Don't forget to trigger the appropriate policy you created above when alarm is raised
# https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/cloudwatch_metric_alarm

# CloudWatch Alarm for Scale Out
resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  alarm_name          = "cpu-utilization-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = "60"
  statistic           = "Average"
  threshold           = "60"
  alarm_description   = "This metric monitors ec2 cpu utilization for scale out"
  alarm_actions       = [aws_autoscaling_policy.scale_out.arn]

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.asg.name
  }
}

# CloudWatch Alarm for Scale In
resource "aws_cloudwatch_metric_alarm" "cpu_low" {
  alarm_name          = "cpu-utilization-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = "1"
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = "60"
  statistic           = "Average"
  threshold           = "50"
  alarm_description   = "This metric monitors ec2 cpu utilization for scale in"
  alarm_actions       = [aws_autoscaling_policy.scale_in.arn]

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.asg.name
  }
}


######################################
# SECOND SECTION ENDS                #
# MAKE SURE YOU COMPLETE ALL 4 STEPS #
######################################
