#!/bin/bash
source /usr/local/src/robot/robotology-superbuild/build/install/share/robotology-superbuild/setup.sh
source /opt/ros/noetic/setup.bash
source /usr/share/gazebo/setup.bash
source /usr/local/src/robot/catkin_ws/devel/setup.bash

exec "$@"