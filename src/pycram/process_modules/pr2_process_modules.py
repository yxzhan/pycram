from threading import Lock
from typing_extensions import Any, Optional, Tuple
from abc import abstractmethod

import actionlib

from .. import world_reasoning as btr
import numpy as np
import rospy


from ..process_module import ProcessModule, ProcessModuleManager
from ..external_interfaces.ik import request_ik
from ..helper import _apply_ik
from ..local_transformer import LocalTransformer
from ..designators.object_designator import ObjectDesignatorDescription
from ..designators.motion_designator import MoveMotion, PickUpMotion, PlaceMotion, LookingMotion, \
    DetectingMotion, MoveTCPMotion, MoveArmJointsMotion, WorldStateDetectingMotion, MoveJointsMotion, \
    MoveGripperMotion, OpeningMotion, ClosingMotion, MotionDesignatorDescription
from ..robot_descriptions import robot_description
from ..world import World
from ..world_object import Object
from ..pose import Pose
from ..enums import JointType, ObjectType
from ..external_interfaces import giskard
from ..external_interfaces.robokudo import query

try:
    from pr2_controllers_msgs.msg import Pr2GripperCommandGoal, Pr2GripperCommandAction, Pr2
except ImportError:
    pass


def _park_arms(arm):
    """
    Defines the joint poses for the parking positions of the arms of PR2 and applies them to the
    in the World defined robot.
    :return: None
    """

    robot = World.robot
    if arm == "right":
        for joint, pose in robot_description.get_static_joint_chain("right", "park").items():
            robot.set_joint_position(joint, pose)
    if arm == "left":
        for joint, pose in robot_description.get_static_joint_chain("left", "park").items():
            robot.set_joint_position(joint, pose)


class Pr2Navigation(ProcessModule):
    """
    The process module to move the robot from one position to another.
    """

    def _execute(self, desig: MoveMotion.Motion):
        robot = World.robot
        robot.set_pose(desig.target)


class Pr2PickUp(ProcessModule):
    """
    This process module is for picking up a given object.
    The object has to be reachable for this process module to succeed.
    """

    def _execute(self, desig: PickUpMotion.Motion, used_arm: Optional[str] = None):
        obj = desig.object_desig.world_object
        robot = World.robot
        grasp = robot_description.grasps.get_orientation_for_grasp(desig.grasp)
        target = obj.get_pose()
        target.orientation.x = grasp[0]
        target.orientation.y = grasp[1]
        target.orientation.z = grasp[2]
        target.orientation.w = grasp[3]

        arm = desig.arm if used_arm is None else used_arm

        _move_arm_tcp(target, robot, arm)
        tool_frame = robot_description.get_tool_frame(arm)
        robot.attach(obj, tool_frame)


class Pr2Place(ProcessModule):
    """
    This process module places an object at the given position in world coordinate frame.
    """

    def _execute(self, desig: PlaceMotion.Motion):
        """

        :param desig: A PlaceMotion
        :return:
        """
        obj = desig.object.world_object
        robot = World.robot
        arm = desig.arm

        # Transformations such that the target position is the position of the object and not the tcp
        object_pose = obj.get_pose()
        local_tf = LocalTransformer()
        tool_name = robot_description.get_tool_frame(arm)
        tcp_to_object = local_tf.transform_pose(object_pose, robot.links[tool_name].tf_frame)
        target_diff = desig.target.to_transform("target").inverse_times(tcp_to_object.to_transform("object")).to_pose()

        _move_arm_tcp(target_diff, robot, arm)
        robot.detach(obj)


class _Pr2MoveHead(ProcessModule):
    """
        This process module moves the head to look at a specific point in the world coordinate frame.
        This point can either be a position or an object.
        """
    def __init__(self, lock: Lock):
        super().__init__(lock)
        self.robot: Object = World.robot

    def get_pan_and_tilt_goals(self, desig: LookingMotion.Motion) -> Tuple[float, float]:
        """
        Calculates the pan and tilt angles to achieve the desired looking motion.
        :param desig: The looking motion designator
        :return: The pan and tilt angles
        """
        target = desig.target

        local_transformer = LocalTransformer()
        pose_in_pan = local_transformer.transform_pose(target, self.robot.links["head_pan_link"].tf_frame)
        pose_in_tilt = local_transformer.transform_pose(target, self.robot.links["head_tilt_link"].tf_frame)

        new_pan = np.arctan2(pose_in_pan.position.y, pose_in_pan.position.x)
        new_tilt = np.arctan2(pose_in_tilt.position.z, pose_in_tilt.position.x ** 2 + pose_in_tilt.position.y ** 2) * -1

        current_pan = self.robot.get_joint_position("head_pan_joint")
        current_tilt = self.robot.get_joint_position("head_tilt_joint")

        return new_pan + current_pan, new_tilt + current_tilt

    @abstractmethod
    def _execute(self, designator: LookingMotion.Motion) -> None:
        pass


class Pr2MoveHead(_Pr2MoveHead):
    """
    This process module moves the head to look at a specific point in the world coordinate frame.
    This point can either be a position or an object.
    """

    def _execute(self, desig: LookingMotion.Motion):
        """
        Moves the head to look at the given position.
        :param desig: The looking motion designator
        """
        pan_goal, tilt_goal = self.get_pan_and_tilt_goals(desig)
        self.robot.set_joint_position("head_pan_joint", pan_goal)
        self.robot.set_joint_position("head_tilt_joint", tilt_goal)


class Pr2MoveGripper(ProcessModule):
    """
    This process module controls the gripper of the robot. They can either be opened or closed.
    Furthermore, it can only moved one gripper at a time.
    """

    def _execute(self, desig: MoveGripperMotion.Motion):
        robot = World.robot
        gripper = desig.gripper
        motion = desig.motion
        for joint, state in robot_description.get_static_gripper_chain(gripper, motion).items():
            robot.set_joint_position(joint, state)


class Pr2Detecting(ProcessModule):
    """
    This process module tries to detect an object with the given type. To be detected the object has to be in
    the field of view of the robot.
    """

    def _execute(self, desig: DetectingMotion.Motion):
        robot = World.robot
        object_type = desig.object_type
        # Should be "wide_stereo_optical_frame"
        cam_frame_name = robot_description.get_camera_frame()
        # should be [0, 0, 1]
        front_facing_axis = robot_description.front_facing_axis

        objects = World.current_world.get_object_by_type(object_type)
        for obj in objects:
            if btr.visible(obj, robot.links[cam_frame_name].pose, front_facing_axis):
                return obj


class Pr2MoveTCP(ProcessModule):
    """
    This process moves the tool center point of either the right or the left arm.
    """

    def _execute(self, desig: MoveTCPMotion.Motion):
        target = desig.target
        robot = World.robot

        _move_arm_tcp(target, robot, desig.arm)


class Pr2MoveArmJoints(ProcessModule):
    """
    This process modules moves the joints of either the right or the left arm. The joint states can be given as
    list that should be applied or a pre-defined position can be used, such as "parking"
    """

    def _execute(self, desig: MoveArmJointsMotion.Motion):

        robot = World.robot
        if desig.right_arm_poses:
            robot.set_joint_positions(desig.right_arm_poses)
        if desig.left_arm_poses:
            robot.set_joint_positions(desig.left_arm_poses)


class PR2MoveJoints(ProcessModule):
    """
    Process Module for generic joint movements, is not confined to the arms but can move any joint of the robot
    """
    def _execute(self, desig: MoveJointsMotion.Motion):
        robot = World.robot
        robot.set_joint_positions(dict(zip(desig.names, desig.positions)))


class Pr2WorldStateDetecting(ProcessModule):
    """
    This process module detectes an object even if it is not in the field of view of the robot.
    """

    def _execute(self, desig: WorldStateDetectingMotion.Motion):
        obj_type = desig.object_type
        return list(filter(lambda obj: obj.obj_type == obj_type, World.current_world.objects))[0]


class Pr2Open(ProcessModule):
    """
    Low-level implementation of opening a container in the simulation. Assumes the handle is already grasped.
    """

    def _execute(self, desig: OpeningMotion.Motion):
        part_of_object = desig.object_part.world_object

        container_joint = part_of_object.find_joint_above_link(desig.object_part.name, JointType.PRISMATIC)

        goal_pose = btr.link_pose_for_joint_config(part_of_object, {
            container_joint: part_of_object.get_joint_limits(container_joint)[1] - 0.05}, desig.object_part.name)

        _move_arm_tcp(goal_pose, World.robot, desig.arm)

        desig.object_part.world_object.set_joint_position(container_joint,
                                                          part_of_object.get_joint_limits(
                                                                  container_joint)[1])


class Pr2Close(ProcessModule):
    """
    Low-level implementation that lets the robot close a grasped container, in simulation
    """

    def _execute(self, desig: ClosingMotion.Motion):
        part_of_object = desig.object_part.world_object

        container_joint = part_of_object.find_joint_above_link(desig.object_part.name, JointType.PRISMATIC)

        goal_pose = btr.link_pose_for_joint_config(part_of_object, {
            container_joint: part_of_object.get_joint_limits(container_joint)[0]}, desig.object_part.name)

        _move_arm_tcp(goal_pose, World.robot, desig.arm)

        desig.object_part.world_object.set_joint_position(container_joint,
                                                          part_of_object.get_joint_limits(
                                                                  container_joint)[0])


def _move_arm_tcp(target: Pose, robot: Object, arm: str) -> None:
    gripper = robot_description.get_tool_frame(arm)

    joints = robot_description.chains[arm].joints

    inv = request_ik(target, robot, joints, gripper)
    _apply_ik(robot, inv, joints)


###########################################################
########## Process Modules for the Real PR2 ###############
###########################################################


class Pr2NavigationReal(ProcessModule):
    """
    Process module for the real PR2 that sends a cartesian goal to giskard to move the robot base
    """

    def _execute(self, designator: MoveMotion.Motion) -> Any:
        rospy.logdebug(f"Sending goal to giskard to Move the robot")
        giskard.achieve_cartesian_goal(designator.target, robot_description.base_link, "map")


class Pr2PickUpReal(ProcessModule):

    def _execute(self, designator: PickUpMotion.Motion) -> Any:
        pass


class Pr2PlaceReal(ProcessModule):

    def _execute(self, designator: MotionDesignatorDescription.Motion) -> Any:
        pass


class Pr2MoveHeadReal(_Pr2MoveHead):
    """
    Process module for the real robot to move that such that it looks at the given position. Uses the same calculation
    as the simulated one
    """

    def _execute(self, desig: LookingMotion.Motion):
        """
        Moves the head to look at the given position.
        :param desig: The looking motion designator
        """
        pan_goal, tilt_goal = self.get_pan_and_tilt_goals(desig)

        giskard.avoid_all_collisions()
        giskard.achieve_joint_goal({"head_pan_joint": pan_goal,
                                    "head_tilt_joint": tilt_goal})


class Pr2DetectingReal(ProcessModule):
    """
    Process Module for the real Pr2 that tries to detect an object fitting the given object description. Uses Robokudo
    for perception of the environment.
    """

    def _execute(self, designator: DetectingMotion.Motion) -> Any:
        query_result = query(ObjectDesignatorDescription(types=[designator.object_type]))
        # print(query_result)
        obj_pose = query_result["ClusterPoseBBAnnotator"]

        lt = LocalTransformer()
        obj_pose = lt.transform_pose(obj_pose, World.robot.links["torso_lift_link"].tf_frame)
        obj_pose.orientation = [0, 0, 0, 1]
        obj_pose.position.x += 0.05

        world_obj = World.current_world.get_object_by_type(designator.object_type)
        if world_obj:
            world_obj[0].set_pose(obj_pose)
            return world_obj[0]
        elif designator.object_type == ObjectType.JEROEN_CUP:
            cup = Object("cup", ObjectType.JEROEN_CUP, "jeroen_cup.stl", pose=obj_pose)
            return cup
        elif designator.object_type == ObjectType.BOWL:
            bowl = Object("bowl", ObjectType.BOWL, "bowl.stl", pose=obj_pose)
            return bowl

        return world_obj[0]


class Pr2MoveTCPReal(ProcessModule):
    """
    Moves the tool center point of the real PR2 while avoiding all collisions
    """

    def _execute(self, designator: MoveTCPMotion.Motion) -> Any:
        lt = LocalTransformer()
        pose_in_map = lt.transform_pose(designator.target, "map")

        if designator.allow_gripper_collision:
            giskard.allow_gripper_collision(designator.arm)
        giskard.achieve_cartesian_goal(pose_in_map, robot_description.get_tool_frame(designator.arm),
                                       robot_description.base_link)


class Pr2MoveArmJointsReal(ProcessModule):
    """
    Moves the arm joints of the real PR2 to the given configuration while avoiding all collisions
    """

    def _execute(self, designator: MoveArmJointsMotion.Motion) -> Any:
        joint_goals = {}
        if designator.left_arm_poses:
            joint_goals.update(designator.left_arm_poses)
        if designator.right_arm_poses:
            joint_goals.update(designator.right_arm_poses)
        giskard.avoid_all_collisions()
        giskard.achieve_joint_goal(joint_goals)


class Pr2MoveJointsReal(ProcessModule):
    """
    Moves any joint using giskard, avoids all collisions while doint this.
    """

    def _execute(self, designator: MoveJointsMotion.Motion) -> Any:
        name_to_position = dict(zip(designator.names, designator.positions))
        giskard.avoid_all_collisions()
        giskard.achieve_joint_goal(name_to_position)


class Pr2MoveGripperReal(ProcessModule):
    """
    Opens or closes the gripper of the real PR2, gripper uses an action server for this instead of giskard 
    """

    def _execute(self, designator: MoveGripperMotion.Motion) -> Any:
        def activate_callback():
            rospy.loginfo("Started gripper Movement")

        def done_callback(state, result):
            rospy.loginfo(f"Reached goal {designator.motion}: {result.reached_goal}")

        def feedback_callback(msg):
            pass

        goal = Pr2GripperCommandGoal()
        goal.command.position = 0.0 if designator.motion == "close" else 0.1
        goal.command.max_effort = 50.0
        if designator.gripper == "right":
            controller_topic = "r_gripper_controller/gripper_action"
        else:
            controller_topic = "l_gripper_controller/gripper_action"
        client = actionlib.SimpleActionClient(controller_topic, Pr2GripperCommandAction)
        rospy.loginfo("Waiting for action server")
        client.wait_for_server()
        client.send_goal(goal, active_cb=activate_callback, done_cb=done_callback, feedback_cb=feedback_callback)
        wait = client.wait_for_result()


class Pr2OpenReal(ProcessModule):
    """
    Tries to open an already grasped container
    """

    def _execute(self, designator: OpeningMotion.Motion) -> Any:
        giskard.achieve_open_container_goal(robot_description.get_tool_frame(designator.arm),
                                            designator.object_part.name)


class Pr2CloseReal(ProcessModule):
    """
    Tries to close an already grasped container
    """

    def _execute(self, designator: ClosingMotion.Motion) -> Any:
        giskard.achieve_close_container_goal(robot_description.get_tool_frame(designator.arm),
                                             designator.object_part.name)


class Pr2Manager(ProcessModuleManager):

    def __init__(self):
        super().__init__("pr2")
        self._navigate_lock = Lock()
        self._pick_up_lock = Lock()
        self._place_lock = Lock()
        self._looking_lock = Lock()
        self._detecting_lock = Lock()
        self._move_tcp_lock = Lock()
        self._move_arm_joints_lock = Lock()
        self._world_state_detecting_lock = Lock()
        self._move_joints_lock = Lock()
        self._move_gripper_lock = Lock()
        self._open_lock = Lock()
        self._close_lock = Lock()

    def navigate(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2Navigation(self._navigate_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2NavigationReal(self._navigate_lock)

    def pick_up(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2PickUp(self._pick_up_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2PickUpReal(self._pick_up_lock)

    def place(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2Place(self._place_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2PlaceReal(self._place_lock)

    def looking(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2MoveHead(self._looking_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2MoveHeadReal(self._looking_lock)

    def detecting(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2Detecting(self._detecting_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2DetectingReal(self._detecting_lock)

    def move_tcp(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2MoveTCP(self._move_tcp_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2MoveTCPReal(self._move_tcp_lock)

    def move_arm_joints(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2MoveArmJoints(self._move_arm_joints_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2MoveArmJointsReal(self._move_arm_joints_lock)

    def world_state_detecting(self):
        if ProcessModuleManager.execution_type == "simulated" or ProcessModuleManager.execution_type == "real":
            return Pr2WorldStateDetecting(self._world_state_detecting_lock)

    def move_joints(self):
        if ProcessModuleManager.execution_type == "simulated":
            return PR2MoveJoints(self._move_joints_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2MoveJointsReal(self._move_joints_lock)

    def move_gripper(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2MoveGripper(self._move_gripper_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2MoveGripperReal(self._move_gripper_lock)

    def open(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2Open(self._open_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2OpenReal(self._open_lock)

    def close(self):
        if ProcessModuleManager.execution_type == "simulated":
            return Pr2Close(self._close_lock)
        elif ProcessModuleManager.execution_type == "real":
            return Pr2CloseReal(self._close_lock)
