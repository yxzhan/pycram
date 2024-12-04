from threading import Lock
from typing import Union

from typing_extensions import Any

import actionlib

from .. import world_reasoning as btr
import numpy as np

from ..process_module import ProcessModule, ProcessModuleManager
from ..external_interfaces.ik import request_ik
from ..ros.logging import logdebug
from ..utils import _apply_ik
from ..local_transformer import LocalTransformer
from ..designators.object_designator import ObjectDesignatorDescription
from ..designators.motion_designator import MoveMotion, LookingMotion, \
    DetectingMotion, MoveTCPMotion, MoveArmJointsMotion, WorldStateDetectingMotion, MoveJointsMotion, \
    MoveGripperMotion, OpeningMotion, ClosingMotion
from ..robot_description import RobotDescription
from ..datastructures.world import World
from ..world_concepts.world_object import Object
from ..datastructures.pose import Pose
from ..datastructures.enums import JointType, ObjectType, Arms, ExecutionType
from ..external_interfaces import giskard
from ..external_interfaces.robokudo import *


import yarp
ACK_VOCAB = yarp.createVocab32('a','c','k')
NO_ACK_VOCAB = yarp.createVocab32('n','a','c','k')

robot_parts = {
        "torso": ["torso_yaw", "torso_roll", "torso_pitch"],
        "right_arm": ["r_shoulder_pitch", "r_shoulder_roll", "r_shoulder_yaw",
                      "r_elbow",
                      "r_wrist_prosup","r_wrist_pitch","r_wrist_yaw",
                      "r_hand_finger",
                      "r_thumb_oppose","r_thumb_proximal","r_thumb_distal",
                      "r_index_proximal","r_index_distal",
                      "r_middle_proximal","r_middle_distal",
                      "r_pinky"],
        "left_arm": ["l_shoulder_pitch", "l_shoulder_roll", "l_shoulder_yaw",
                     "l_elbow",
                     "l_wrist_prosup","l_wrist_pitch","l_wrist_yaw",
                     "l_hand_finger",
                     "l_thumb_oppose","l_thumb_proximal","l_thumb_distal",
                     "l_index_proximal","l_index_distal",
                     "l_middle_proximal","l_middle_distal",
                     "l_pinky"],
    }

# Convert to a list of parts for indexed access
robot_parts_list = list(robot_parts.items())


def init_yarp_network():
    if not yarp.Network.checkNetwork():
        print("Unable to find a yarp server exiting ...")
        return False

    yarp.Network.init()
    return True

def open_rpc_client_port(port_name):
    handle_port: yarp.RpcClient = yarp.RpcClient()
    if not handle_port.open(port_name):
        print(f"Can't open the port %s correctly" % port_name)
        return False , None
    print(f"Port %s opened correctly" % port_name)
    return True , handle_port

def open_buffered_bottle_port(port_name):
    opened_port: yarp.BufferedPortBottle = yarp.BufferedPortBottle()
    if not opened_port.open(port_name):
        print(f"Can't open the port %s correctly" % port_name)
        return False , None
    print(f"Port %s opened correctly" % port_name)
    return True , opened_port


def update_part(state_port,ctp_port, joint_to_change_idx, joints_to_change_pos):
    if len(joint_to_change_idx):
        part_state: yarp.Bottle = state_port.read(shouldWait=True)
        part_new_states = []
        for i in range(part_state.size()):
            print(i, "   ", part_state.get(i).asFloat32())
            part_new_states.append(part_state.get(i).asFloat32())

        for i in range(len(joint_to_change_idx)):
            part_new_states[joint_to_change_idx[i]] = joints_to_change_pos[i]

        yarp_bottle_msg: yarp.Bottle = yarp.Bottle()
        yarp_bottle_reply: yarp.Bottle = yarp.Bottle()
        yarp_bottle_msg.addVocab32('c', 't', 'p', 'q')
        yarp_bottle_msg.addVocab32('t', 'i', 'm', 'e')
        yarp_bottle_msg.addFloat32(1.5)
        yarp_bottle_msg.addVocab32('o', 'f', 'f')
        yarp_bottle_msg.addInt32(0)
        yarp_bottle_msg.addVocab32('p', 'o', 's')
        target_loc: yarp.Bottle = yarp_bottle_msg.addList()
        for i in part_new_states:
            target_loc.addFloat32(i)

        print(f"command Ready to send to iCub torso tcp")

        ctp_port.write(yarp_bottle_msg, yarp_bottle_reply)
        reply_vocab = yarp_bottle_reply.get(0).asVocab32()

        if reply_vocab == NO_ACK_VOCAB:
            print("NO_ACK")
            return False
        elif reply_vocab == ACK_VOCAB:
            print("ACK")
            return True
        else:
            print("another reply")
            return False

class iCubNavigation(ProcessModule):
    """
    The process module to move the robot from one position to another.
    """

    def _execute(self, desig: MoveMotion):
        print("iCubNavigate")


class iCubMoveHead(ProcessModule):
    """
        This process module moves the head to look at a specific point in the world coordinate frame.
        This point can either be a position or an object.
        """

    def __init__(self,lock,cmd_port:yarp.RpcClient):
        super().__init__(lock)
        self.cmd_port = cmd_port

    def _execute(self, desig: LookingMotion):
        print("iCub Move Head")
        position_target = desig.target.pose.position
        if self.cmd_port.getOutputCount():

            yarp_bottle_msg: yarp.Bottle = yarp.Bottle()
            yarp_bottle_reply: yarp.Bottle = yarp.Bottle()
            yarp_bottle_msg.addVocab32('l', 'o', 'o', 'k')
            yarp_bottle_msg.addVocab32('3', 'D')

            target_loc: yarp.Bottle = yarp_bottle_msg.addList()
            target_loc.addFloat32(position_target.x)
            target_loc.addFloat32(position_target.y)
            target_loc.addFloat32(position_target.z)
            print(f"command Ready to send to iCub")
            self.cmd_port .write(yarp_bottle_msg, yarp_bottle_reply)
            reply_vocab = yarp_bottle_reply.get(0).asVocab32()

            if reply_vocab == NO_ACK_VOCAB:
                print("NO_ACK")
                return False
            elif reply_vocab == ACK_VOCAB:
                print("ACK")
                return True
            else:
                print("another reply")
                return False


        else:
            print("port is not connected")
            return False



class iCubMoveGripper(ProcessModule):
    """
    This process module controls the gripper of the robot. They can either be opened or closed.
    Furthermore, it can only moved one gripper at a time.
    """

    def _execute(self, desig: MoveGripperMotion):
        print("iCub Move Gripper")


class iCubDetecting(ProcessModule):
    """
    This process module tries to detect an object with the given type. To be detected the object has to be in
    the field of view of the robot.
    """

    def _execute(self, desig: DetectingMotion):
        print("iCub Detect")

class iCubMoveTCP(ProcessModule):
    """
    This process moves the tool center point of either the right or the left arm.
    """

    def __init__(self, lock, cmd_port: yarp.RpcClient):
        super().__init__(lock)
        self.cmd_port = cmd_port

    def _execute(self, desig: MoveTCPMotion):
        print("iCub Move Head")
        position_target = desig.target.position
        if self.cmd_port.getOutputCount():

            yarp_bottle_msg: yarp.Bottle = yarp.Bottle()
            yarp_bottle_reply: yarp.Bottle = yarp.Bottle()
            yarp_bottle_msg.addVocab32('t', 'o', 'u', 'c')

            target_loc: yarp.Bottle = yarp_bottle_msg.addList()
            target_loc.addFloat32(position_target.x)
            target_loc.addFloat32(position_target.y)
            target_loc.addFloat32(position_target.z)
            print(f"command Ready to send to iCub")
            yarp_bottle_msg.addString("side")
            if desig.arm == Arms.LEFT:
                yarp_bottle_msg.addString("left")
            elif desig.arm == Arms.RIGHT:
                yarp_bottle_msg.addString("right")

            yarp_bottle_msg.addString("still")

            self.cmd_port.write(yarp_bottle_msg, yarp_bottle_reply)
            reply_vocab = yarp_bottle_reply.get(0).asVocab32()

            if reply_vocab == NO_ACK_VOCAB:
                print("NO_ACK")
                return False
            elif reply_vocab == ACK_VOCAB:
                print("ACK")
                return True
            else:
                print("another reply")
                return False


        else:
            print("port is not connected")
            return False




class iCubMoveArmJoints(ProcessModule):
    """
    This process modules moves the joints of either the right or the left arm. The joint states can be given as
    list that should be applied or a pre-defined position can be used, such as "parking"
    """

    def _execute(self, desig: MoveArmJointsMotion):
        print("iCub Move Arm Joints")

class iCubMoveJoints(ProcessModule):
    """
    Process Module for generic joint movements, is not confined to the arms but can move any joint of the robot
    """

    def __init__(self, lock,
                 state_ports : [yarp.BufferedPortBottle],
                 ctp_ports: [yarp.RpcClient]):
        super().__init__(lock)
        self.state_ports = state_ports
        self.ctp_ports = ctp_ports

    def get_joint_indices(self,joint_name):
        """
        Given a joint name, returns the indices of the part and the joint within that part.

        Args:
            joint_name (str): Name of the joint to find.

        Returns:
            tuple: (part_index, joint_index) or (None, None) if joint not found.
        """
        left_arm_chain = RobotDescription.current_robot_description.get_arm_chain(Arms.LEFT)
        print(left_arm_chain.joint_names)
        right_arm_chain = RobotDescription.current_robot_description.get_arm_chain(Arms.RIGHT)
        print(right_arm_chain.joint_names)
        for part_index, (part_name, joints) in enumerate(robot_parts_list):
            if joint_name in joints:
                joint_index = joints.index(joint_name)
                return part_index, joint_index
        return None, None


    def _execute(self, desig: MoveJointsMotion):
        torso_to_change_joints = []
        right_arm_to_change_joints = []
        left_arm_to_change_joints = []

        torso_to_change_joints_states = []
        right_arm_to_change_joints_states = []
        left_arm_to_change_joints_states = []

        to_change_joints = desig.names
        to_change_states = desig.positions

        index = 0
        for joint_mame in to_change_joints:
            part_index,joint_index = self.get_joint_indices(joint_mame)
            if part_index is not None:
                if part_index == 0:
                    torso_to_change_joints.append(joint_index)
                    torso_to_change_joints_states.append(to_change_states[index])
                elif part_index == 1:
                    right_arm_to_change_joints.append(joint_index)
                    right_arm_to_change_joints_states.append(to_change_states[index])
                elif part_index == 2:
                    left_arm_to_change_joints.append(joint_index)
                    left_arm_to_change_joints_states.append(to_change_states[index])
                else:
                    print("error in index")

            index += 1

        update_part(self.state_ports[0],
                    self.ctp_ports[0],
                    torso_to_change_joints,
                    torso_to_change_joints_states)

        update_part(self.state_ports[1],
                    self.ctp_ports[1],
                    right_arm_to_change_joints,
                    right_arm_to_change_joints_states)

        update_part(self.state_ports[2],
                    self.ctp_ports[2],
                    left_arm_to_change_joints,
                    left_arm_to_change_joints_states)



    print("iCub Move Joints")


class iCubWorldStateDetecting(ProcessModule):
    """
    This process module detectes an object even if it is not in the field of view of the robot.
    """

    def _execute(self, desig: WorldStateDetectingMotion):
        print("iCub World State Detecting")


class iCubOpen(ProcessModule):
    """
    Low-level implementation of opening a container in the simulation. Assumes the handle is already grasped.
    """

    def _execute(self, desig: OpeningMotion):
        print("iCub Open")


class iCubClose(ProcessModule):
    """
    Low-level implementation that lets the robot close a grasped container, in simulation
    """

    def _execute(self, desig: ClosingMotion):
        print("iCub Close")



###########################################################
########## Process Modules for the Real iCub ##############
###########################################################


class iCubNavigationReal(ProcessModule):
    """
    Process module for the real PR2 that sends a cartesian goal to giskard to move the robot base
    """

    def _execute(self, designator: MoveMotion) -> Any:
        print("iCub Navigate Real")


class iCubDetectingReal(ProcessModule):
    """
    Process Module for the real Pr2 that tries to detect an object fitting the given object description. Uses Robokudo
    for perception of the environment.
    """

    def _execute(self, designator: DetectingMotion) -> Any:
        print("iCub Detecting Real")


class iCubMoveTCPReal(ProcessModule):
    """
    Moves the tool center point of the real PR2 while avoiding all collisions
    """

    def _execute(self, designator: MoveTCPMotion) -> Any:
        print("iCub Move TCP Real")


class iCubMoveArmJointsReal(ProcessModule):
    """
    Moves the arm joints of the real iCub to the given configuration while avoiding all collisions
    """

    def _execute(self, designator: MoveArmJointsMotion) -> Any:
        print("iCub Move Arm Joints Real")


class iCubMoveJointsReal(ProcessModule):
    """
    Moves any joint using giskard, avoids all collisions while doint this.
    """

    def _execute(self, designator: MoveJointsMotion) -> Any:
        print("iCub Move Joints Real")


class iCubMoveGripperReal(ProcessModule):
    """
    Opens or closes the gripper of the real PR2, gripper uses an action server for this instead of giskard 
    """

    def _execute(self, designator: MoveGripperMotion) -> Any:
        print("iCub Move Gripper Real")


class iCubOpenReal(ProcessModule):
    """
    Tries to open an already grasped container
    """

    def _execute(self, designator: OpeningMotion) -> Any:
        print("iCub Open Real")


class iCubCloseReal(ProcessModule):
    """
    Tries to close an already grasped container
    """

    def _execute(self, designator: ClosingMotion) -> Any:
        print("iCub Close Real")


class ICubManager(ProcessModuleManager):

    def __init__(self):
        super().__init__("icub")
        self._navigate_lock = Lock()
        self._looking_lock = Lock()
        self._detecting_lock = Lock()
        self._move_tcp_lock = Lock()
        self._move_arm_joints_lock = Lock()
        self._world_state_detecting_lock = Lock()
        self._move_joints_lock = Lock()
        self._move_gripper_lock = Lock()
        self._open_lock = Lock()
        self._close_lock = Lock()
        self.yarp_network_state = init_yarp_network()

        # yarp related
        self.robot_name_yarp = "icubSim"
        self.gaze_cmd_port_name = "/pycram/gaze/cmd:oi"
        self.action_cmd_port_name = "/pycram/action/cmd:oi"

        self.ctp_torso_client_port_name = "/pycram/ctp/torso:oi"
        self.ctp_right_arm_client_port_name = "/pycram/ctp/right_arm:oi"
        self.ctp_left_arm_client_port_name = "/pycram/ctp/left_arm:oi"

        self.state_torso_port_name = "/pycram/ctp/torso:i"
        self.state_right_arm_port_name = "/pycram/ctp/right_arm:i"
        self.state_left_arm_port_name = "/pycram/ctp/left_arm:i"

        self.gaze_client_port = None
        self.action_client_port = None

        self.ctp_torso_client_port = None
        self.ctp_right_arm_client_port = None
        self.ctp_left_arm_client_port = None

        self.state_torso_port =  yarp.BufferedPortBottle()
        self.state_right_arm_port =  yarp.BufferedPortBottle()
        self.state_left_arm_port =  yarp.BufferedPortBottle()
        self.initialized = False
        if self.yarp_network_state:
            print("yarp network state detected")
            self.config_yarp_ports()
            self.connect_yarp_ports()
            self.initialized = True
        else:
            print("yarp network state not detected")







    def navigate(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            print('Navigate iCub')
            return iCubNavigation(self._navigate_lock)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubNavigationReal(self._navigate_lock)

    def looking(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubMoveHead(self._looking_lock,self.gaze_client_port)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubMoveHead(self._looking_lock,self.gaze_client_port)

    def detecting(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubDetecting(self._detecting_lock)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubDetectingReal(self._detecting_lock)

    def move_tcp(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubMoveTCP(self._move_tcp_lock,self.action_client_port)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubMoveTCPReal(self._move_tcp_lock)

    def move_arm_joints(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubMoveArmJoints(self._move_arm_joints_lock)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubMoveArmJointsReal(self._move_arm_joints_lock)

    def world_state_detecting(self):
        if (ProcessModuleManager.execution_type == ExecutionType.SIMULATED or
                ProcessModuleManager.execution_type == ExecutionType.REAL):
            return iCubWorldStateDetecting(self._world_state_detecting_lock)

    def move_joints(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubMoveJoints(self._move_joints_lock,
                                  [self.state_torso_port,self.state_right_arm_port,self.state_right_arm_port],
                                  [self.ctp_torso_client_port,self.ctp_right_arm_client_port,self.ctp_right_arm_client_port])
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubMoveJointsReal(self._move_joints_lock)

    def move_gripper(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubMoveGripper(self._move_gripper_lock)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubMoveGripperReal(self._move_gripper_lock)

    def open(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubOpen(self._open_lock)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubOpenReal(self._open_lock)

    def close(self):
        if ProcessModuleManager.execution_type == ExecutionType.SIMULATED:
            return iCubClose(self._close_lock)
        elif ProcessModuleManager.execution_type == ExecutionType.REAL:
            return iCubCloseReal(self._close_lock)


    def config_yarp_ports(self)->bool:
        suc, self.gaze_client_port = open_rpc_client_port(self.gaze_cmd_port_name)
        if not suc:
            print("Failed to open, ", self.gaze_cmd_port_name)
            return False
        suc, self.action_client_port = open_rpc_client_port(self.action_cmd_port_name)
        if not suc:
            print("Failed to open, ", self.action_cmd_port_name)
            return False

        suc, self.ctp_torso_client_port = open_rpc_client_port(self.ctp_torso_client_port_name)
        if not suc:
            print("Failed to open, ", self.ctp_torso_client_port_name)
            return False

        suc, self.ctp_right_arm_client_port = open_rpc_client_port(self.ctp_right_arm_client_port_name)
        if not suc:
            print("Failed to open, ", self.ctp_right_arm_client_port_name)
            return False

        suc, self.ctp_left_arm_client_port = open_rpc_client_port(self.ctp_left_arm_client_port_name)
        if not suc:
            print("Failed to open, ", self.ctp_left_arm_client_port_name)
            return False

        suc, self.state_torso_port = open_buffered_bottle_port(self.state_torso_port_name)
        if not suc:
            print("Failed to open, ", self.state_torso_port_name)
            return False

        suc, self.state_right_arm_port = open_buffered_bottle_port(self.state_right_arm_port_name)
        if not suc:
            print("Failed to open, ", self.state_right_arm_port_name)
            return False

        suc, self.state_left_arm_port = open_buffered_bottle_port(self.state_left_arm_port_name)
        if not suc:
            print("Failed to open, ", self.state_left_arm_port_name)
            return False



        return True

    def connect_yarp_ports(self)->bool:
        connection_status = yarp.NetworkBase_connect(self.gaze_cmd_port_name, "/iKinGazeCtrl/rpc", "tcp")
        if not connection_status:
            print("gaze control port couldn't connect")

        connection_status = yarp.NetworkBase_connect(self.action_cmd_port_name, "/actionsRenderingEngine/cmd:io", "tcp")
        if not connection_status:
            print("action control port couldn't connect")
            return False

        # ctp service ports
        connection_status = yarp.NetworkBase_connect(self.ctp_torso_client_port_name, "/ctpservice/torso/rpc", "tcp")
        #connection_status = yarp.NetworkBase_connect(self.ctp_torso_client_port_name, "/testrpc", "tcp")

        if not connection_status:
            print("action control port couldn't connect")
            return False

        connection_status = yarp.NetworkBase_connect(self.ctp_right_arm_client_port_name, "/ctpservice/right_arm/rpc", "tcp")
        if not connection_status:
            print("action control port couldn't connect")
            return False

        connection_status = yarp.NetworkBase_connect( self.ctp_left_arm_client_port_name,"/ctpservice/left_arm/rpc", "tcp")
        if not connection_status:
            print("action control port couldn't connect")
            return False

        # status ports
        connection_status = yarp.NetworkBase_connect("/"+self.robot_name_yarp+"/torso/state:o", self.state_torso_port_name, "tcp")
        if not connection_status:
            print("action control port couldn't connect")
            return False

        connection_status = yarp.NetworkBase_connect("/"+self.robot_name_yarp+"/right_arm/state:o",self.state_right_arm_port_name,  "tcp")
        if not connection_status:
            print("action control port couldn't connect")
            return False

        connection_status = yarp.NetworkBase_connect("/"+self.robot_name_yarp+"/left_arm/state:o",self.state_left_arm_port_name , "tcp")
        if not connection_status:
            print("action control port couldn't connect")
            return False

        return True


    def exit(self):
        if self.initialized:
            self.disconnect_and_remove()


    def disconnect_and_remove(self):
        self.gaze_client_port .interrupt()
        self.action_client_port.interrupt()
        self.ctp_torso_client_port.interrupt()
        self.ctp_right_arm_client_port.interrupt()
        self.ctp_left_arm_client_port.interrupt()
        self.state_torso_port.interrupt()
        self.state_right_arm_port.interrupt()
        self.state_left_arm_port.interrupt()

        self.gaze_client_port .close()
        self.action_client_port.close()
        self.ctp_torso_client_port.close()
        self.ctp_right_arm_client_port.close()
        self.ctp_left_arm_client_port.close()
        self.state_torso_port.close()
        self.state_right_arm_port.close()
        self.state_left_arm_port.close()


