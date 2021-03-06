#!/usr/bin/env python
import rospy

# Imports for bag recording
import rosbag
import subprocess
import os
import sys
import signal
import time

from std_msgs.msg import Int16, Float32
from sensor_msgs.msg import Imu, Joy
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry

import numpy as np

'''
A listener that subscribes to the node publishing the current surface.  When the surface changes, check to see if the
new one is already known.  If so, update the command parameter for max speed to the previously discovered value for 
this surface.  If the surface is unknown, create a temporary entry with the default value. If the vibrations exceed the specified 
threshold, reduce the max speed parameter for the current surface.
'''

default_dict = {"max_x_vel" : 2.0, "max_accel" : 0.25}
#dictionary of surfaces that have been discovered
#keys are integers representing a surface
surface_data = {-1 : default_dict.copy()}
#keep track of the previous surface so each iteration can compare to it
current_surface = -1
previous_surface = -1

# bumpiness tracking
previous_bumpiness = 0
current_bumpiness = 0
upcoming_bumpiness = False # if this is true the upcoming surface is considerably bumpier than the current one
surface_transition = False # if this is true the upcoming surface is considerably less bumpy than the current one
start_position = 0 # we will measure the distance from this point to determine when we have reached the upcoming bumpy or smooth surface

accels = []

current_commanded_velocity = 0

#current amount of vertical vibration the robot is experiencing
#updated by the z acceleration recorded by the IMU
z_vibrations = -1
#The maximum amount of vibration the robot is allowed to experience
vibration_threshold = 0.45

previous_stamp = 0
previous_x_velocity = 0
older_x_velocity = 0

speed_publisher = rospy.Publisher('/cmd_vel', Twist, queue_size=10)

### PID Variables ####
previous_error = 0.0

kp = 1
ki = 0.001
kd = 0.00001

p_error = 0.0
i_error = 0.0
d_error = 0.0

current_x_vel = 0.0

delta_t = 0.0

previous_odometry_reading_time = 0.0

'''
Callback to run every time a new surface is published to the surface_detection topic
'''
def surface_callback(data):
    global current_surface
    global previous_surface
    global surface_data
    global previous_bumpiness
    global current_bumpiness

    previous_surface = current_surface
    current_surface = data.data

    # check to make sure the current surface isn't already known
    if current_surface not in surface_data:
	# if the previous surface was unclassified (-1), the parameters for the -1 entry have been updating
	# and need to be stored in the dictionary
	# otherwise set the new entry to the default values
        if previous_surface == -1:
	    # set the values for the new entry to the current values for -1
            surface_data[current_surface] = surface_data[-1].copy()
	    # and then reset the values for -1 back to the defaults
            surface_data[-1] = default_dict.copy()
        else:
	    surface_data[current_surface] = default_dict.copy()
    else:
	   surface_data[-1] = default_dict.copy()

'''
Callback to run every time a new standard deviation of a set of z acceleration measurements
is published to the surface_bumpiness topic
'''
def bumpiness_callback(data):
    global previous_bumpiness
    global current_bumpiness
    global upcoming_bumpiness
    global surface_transition

    previous_bumpiness = current_bumpiness
    current_bumpiness = data.data

    # Check to see if the upcoming surface is substantially bumpier than the current one
    if current_bumpiness > previous_bumpiness * 1.75 and previous_bumpiness != 0:
        upcoming_bumpiness = True

    # Check to see if the upcoming surface is substantially less bumpy than the current one
    if current_bumpiness < previous_bumpiness / 1.75 and previous_bumpiness != 0:
        surface_transition = True

'''
Callback to run every time a new message is published to the /odometry/filtered topic
'''
def odometry_callback(data):
    global current_x_vel
    global delta_t
    global previous_odometry_reading_time
    global upcoming_bumpiness
    global start_position
    global surface_transition

    # get the velocity message out of the data
    velocity_msg = data.twist.twist
    # get the x velocity
    current_x_vel = velocity_msg.linear.x
    # calculate the time difference between the previous velocity reading and this current one
    current_seconds = data.header.stamp.secs
    current_nanoseconds = data.header.stamp.nsecs
    current_stamp = (10**-9) * current_nanoseconds + current_seconds
    delta_t = current_stamp - previous_odometry_reading_time
    # and update the global previous time to be used on the next iteration
    previous_odometry_reading_time = current_stamp

    # distance tracking for upcoming surface
    lidar_look_ahead_distance_m = .45
    current_position = data.pose.pose.position.x

    # if either upcoming_bumpiness or surface_transition are true it is likely we are about to change surfaces
    # make sure start_position is zero to ensure we aren't already preparing for this transition
    # if not update the start position to the current position of the robot so we can start keeping track of the distance travelled
    if (upcoming_bumpiness or surface_transition) and start_position == 0:
        start_position = current_position

    # measure the distance travelled since the start position
    # if it is greater than the lidar look ahead distance value, we have made it to the next surface, so reset the 
    # upcoming_bumpiness and surface_transition parameters and reset the start position back to zero
    if start_position != 0 and abs(current_position - start_position) >= lidar_look_ahead_distance_m:
        upcoming_bumpiness = False
        surface_transition = False
        start_position = 0

'''
Callback to run every time a new message is published to the joystick topic
'''
def joystick_callback(data):
    global previous_stamp
    global previous_x_velocity
    global older_x_velocity
    global speed_publisher
    global current_surface
    global previous_surface
    global default_dict
    global upcoming_bumpiness

    # the twist message that will be sent to the robot
    # this will be the joystick command after being modified by the control adaptation algorithm
    output_velocity = Twist()

    # If R2 is not pressed down, don't move robot
    if data.buttons[9] == 0:
        return
    # read the command from the joystick
    input_x_velocity = data.axes[1] * -2
    input_x_direction = 1
    # set the reverse flag if the velocity is negative
    if input_x_velocity < 0:
        input_x_direction = -1
    # get the magnitude of the velocity
    input_x_velocity = abs(input_x_velocity)

    # if the upcoming_bumpiness flag is true, meaning the upcoming surface is considerably bumpier than the current one
    # start slowing down by setting the input velocity really low
    if upcoming_bumpiness and input_x_velocity > 0.2:
        input_x_velocity = 0.2

    # get the maximum speed parameter for the current surface from the dictionary
    surface_dictionary = surface_data[current_surface]
    output_x_velocity = min(surface_dictionary["max_x_vel"], input_x_velocity) * input_x_direction

    current_seconds = data.header.stamp.secs
    current_nanoseconds = data.header.stamp.nsecs
    current_stamp = (10**-9) * current_nanoseconds + current_seconds
    dt = current_stamp - previous_stamp

    if previous_x_velocity > 2 or previous_x_velocity < -2:
        previous_x_velocity
        sys.quit()

    if dt == 0 or previous_stamp == 0:
        output_velocity.linear.x = previous_x_velocity
        speed_publisher.publish(output_velocity)
        previous_stamp = current_stamp
        return

    previous_stamp = current_stamp
    dv = output_x_velocity - previous_x_velocity
    a = dv/dt

    if abs(a) > surface_dictionary["max_accel"]:
        adjusted_a = surface_dictionary["max_accel"]
        if a < 0:
            adjusted_a *= -1
        output_x_velocity = adjusted_a*dt + previous_x_velocity

    adjusted_velocity = calculate_pid(output_x_velocity)
    output_velocity.linear.x = adjusted_velocity

    older_x_velocity = previous_x_velocity
    previous_x_velocity = adjusted_velocity

    #if (output_velocity.linear.x > 2) or (output_velocity.linear.x < -2):
    #    output_velocity.linear.x = 0
    #    print("ERROR: Speed too high")
    #    sys.exit(1)

    speed_publisher.publish(output_velocity)

def imu_callback(data):
    global accels
    global z_vibrations
    global surface_data
    global current_surface
    global previous_x_velocity
    global older_x_velocity
    global surface_transition

    #print(surface_data)
    z_vibrations = data.linear_acceleration.z
    accels.append(z_vibrations)
    if len(accels) >= 20:
        std_dev = np.std(accels)
        accels = []
        if std_dev > vibration_threshold and abs(older_x_velocity) < abs(previous_x_velocity) and not surface_transition:
            current_max = surface_data[current_surface]["max_x_vel"]
            surface_data[current_surface]["max_x_vel"] = min(abs(previous_x_velocity), current_max) - .15
	    #print("SURFACE " + str(current_surface) + ": UPDATING MAX FROM " + str(current_max) + " to " + str(surface_data[current_surface]["max_x_vel"]))


def calculate_pid(target_velocity):
    global current_x_vel
    global previous_error
    global delta_t
    global p_error
    global i_error
    global d_error
    global kp
    global ki
    global kd

    p_error = target_velocity - current_x_vel
    i_error += p_error*delta_t

    if delta_t > 0:
        d_error = (p_error - previous_error)/delta_t

    previous_error = p_error
    return kp*p_error + ki*i_error + kd*d_error + current_x_vel


'''
Main loop of the listener
'''
def listener():

    # In ROS, nodes are uniquely named. If two nodes with the same
    # name are launched, the previous one is kicked off. The
    # anonymous=True flag means that rospy will choose a unique
    # name for our 'listener' node so that multiple listeners can
    # run simultaneously.
    rospy.init_node('surface_listener', anonymous=True)

    rospy.Subscriber('surface_detection', Int16, surface_callback)
    rospy.Subscriber('surface_bumpiness', Float32, bumpiness_callback)

    #subscribe to the IMU to update the z_vibrations variable
    rospy.Subscriber("/imu/data", Imu, imu_callback)

    #subscribe to the IMU to update the z_vibrations variable
    rospy.Subscriber("/bluetooth_teleop/joy", Joy, joystick_callback)

    rospy.Subscriber("/odometry/filtered", Odometry, odometry_callback)
	
    while not rospy.is_shutdown():
        pass


if __name__ == '__main__':
    listener()
