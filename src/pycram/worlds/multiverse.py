import logging
import os
from time import time

from typing_extensions import List, Dict, Optional

from ..datastructures.dataclasses import AxisAlignedBoundingBox, Color
from ..datastructures.enums import WorldMode, JointType
from ..datastructures.pose import Pose
from ..description import Link, Joint
from ..world import World
from ..world_concepts.constraints import Constraint
from ..world_concepts.multiverse_socket import MultiverseSocket, SocketAddress
from ..world_concepts.world_object import Object


def get_resource_paths(dirname: str) -> List[str]:
    resources_paths = ["../robots", "../worlds", "../objects"]
    resources_paths = [
        os.path.join(dirname, resources_path.replace('../', '')) if not os.path.isabs(
            resources_path) else resources_path
        for resources_path in resources_paths
    ]

    def add_directories(path: str) -> List[str]:
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_dir():
                    resources_paths.append(entry.path)
                    add_directories(entry.path)

    resources_path_copy = resources_paths.copy()
    for resources_path in resources_path_copy:
        add_directories(resources_path)

    return resources_paths


def find_multiverse_resources_path() -> Optional[str]:
    """
    Find the path to the Multiverse resources directory.
    """
    # Get the path to the Multiverse installation
    multiverse_path = find_multiverse_path()

    # Check if the path to the Multiverse installation was found
    if multiverse_path:
        # Construct the path to the resources directory
        resources_path = os.path.join(multiverse_path, 'resources')

        # Check if the resources directory exists
        if os.path.exists(resources_path):
            return resources_path

    return None


def find_multiverse_path() -> Optional[str]:
    """
    Find the path to the Multiverse installation.
    """
    # Get the value of PYTHONPATH environment variable
    pythonpath = os.getenv('PYTHONPATH')

    # Check if PYTHONPATH is set
    if pythonpath:
        # Split the PYTHONPATH into individual paths using the platform-specific path separator
        paths = pythonpath.split(os.pathsep)

        # Iterate through each path and check if 'Multiverse' is in it
        for path in paths:
            if 'multiverse' in path:
                multiverse_path = path.split('multiverse')[0]
                return multiverse_path + 'multiverse'

    return None


class Multiverse(MultiverseSocket, World):
    """
    This class implements an interface between Multiverse and PyCRAM.
    """

    _joint_type_to_position_name: Dict[JointType, str] = {
        JointType.REVOLUTE: "joint_rvalue",
        JointType.PRISMATIC: "joint_tvalue",
    }
    """
    A dictionary to map JointType to the corresponding multiverse attribute name.
    """

    added_multiverse_resources: bool = False
    """
    A flag to check if the multiverse resources have been added.
    """

    def __init__(self, simulation: str, mode: Optional[WorldMode] = WorldMode.DIRECT,
                 is_prospection: Optional[bool] = False,
                 simulation_frequency: Optional[float] = 60.0,
                 client_addr: Optional[SocketAddress] = SocketAddress(port="7000")):
        """
        Initialize the Multiverse Socket and the PyCram World.
        param mode: The mode of the world (DIRECT or GUI).
        param is_prospection: Whether the world is prospection or not.
        param simulation_frequency: The frequency of the simulation.
        param client_addr: The address of the multiverse client.
        """
        MultiverseSocket.__init__(self, client_addr)
        World.__init__(self, mode, is_prospection, simulation_frequency)
        self.simulation: str = simulation
        self._make_sure_multiverse_resources_are_added()
        self.last_object_id: int = -1
        self.time_start = time()
        self.run()

    def _make_sure_multiverse_resources_are_added(self):
        """
        Add the multiverse resources to the pycram world resources.
        """
        if not self.added_multiverse_resources:
            dirname = find_multiverse_resources_path()
            resources_paths = get_resource_paths(dirname)
            for resource_path in resources_paths:
                self.add_resource_path(resource_path)
            self.added_multiverse_resources = True

    def get_joint_position_name(self, joint: Joint) -> str:
        if joint.type not in self._joint_type_to_position_name:
            logging.warning(f"Invalid joint type: {joint.type}")
            return "joint_rvalue"
        return self._joint_type_to_position_name[joint.type]

    def load_object_and_get_id(self, path: str, pose: Optional[Pose] = None) -> int:
        """
        This is a placeholder until a proper spawning mechanism is available in Multiverse.
        param path: The path is used as the name of the object.
        param pose: The pose of the object.
        """
        if pose is None:
            pose = Pose()

        name = path.split('/')[-1]
        self.request_meta_data["meta_data"]["simulation_name"] = self._meta_data.simulation_name
        self.request_meta_data["send"][path] = ["position",
                                                "quaternion",
                                                "relative_velocity"]
        self.send_and_receive_meta_data()

        time_now = time() - self.time_start
        self.send_data = [time_now,
                          0, 0, 5,
                          0.0, 0.0, 0.0, 1.0,
                          0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                          0, 0, 3,
                          0.0, 0.0, 0.0, 1.0]
        self.send_and_receive_data()

        self._reset_body_pose(path, pose)

        self.last_object_id += 1

        return self.last_object_id

    def get_object_joint_names(self, obj: Object) -> List[str]:
        return [joint.name for joint in obj.description.joints]

    def get_object_link_names(self, obj: Object) -> List[str]:
        return [link.name for link in obj.description.links]

    def _init_getter(self):
        self.request_meta_data["receive"] = {}
        self.request_meta_data["send"] = {}
        self.request_meta_data["meta_data"]["simulation_name"] = self._meta_data.simulation_name

    def get_joint_position(self, joint: Joint) -> float:
        self._init_getter()
        attribute = self.get_joint_position_name(joint)
        self.request_meta_data["receive"][joint.name] = [attribute]
        self.send_and_receive_meta_data()
        receive_data = self.response_meta_data["receive"][joint.name][attribute]
        if len(receive_data) != 1:
            logging.error(f"Invalid joint position data: {receive_data}")
            raise ValueError
        return receive_data[0]

    def _init_setter(self):
        self.request_meta_data["send"] = {}
        self.request_meta_data["receive"] = {}
        self.request_meta_data["meta_data"]["simulation_name"] = self.simulation

    def reset_joint_position(self, joint: Joint, joint_position: float) -> None:
        self._init_setter()
        self.send_and_receive_meta_data()
        attribute = self.get_joint_position_name(joint)
        self.request_meta_data["send"][joint.name] = [attribute]
        self.send_data = [time(), joint_position]
        self.send_and_receive_data()

    def get_link_pose(self, link: Link) -> Pose:
        return self._get_body_pose(link.name)

    def get_object_pose(self, obj: Object) -> Pose:
        return self._get_body_pose(obj.name)

    def _get_body_pose(self, body_name: str) -> Pose:
        self._init_getter()
        self.request_meta_data["receive"][body_name] = ["position", "quaternion"]
        self._communicate(True)
        self._communicate()
        if len(self.receive_data) != 8:
            logging.error(f"Invalid body pose data: {self.receive_data}")
            raise ValueError
        return Pose(self.receive_data[1:4], self.receive_data[4:])

    def reset_object_base_pose(self, obj: Object, pose: Pose):
        self._reset_body_pose(obj.name, pose)

    def _reset_body_pose(self, body_name: str, pose: Pose):
        """
        Reset the pose of a body in the simulator.
        """
        self.request_meta_data["send"] = {}
        self.request_meta_data["receive"] = {}
        self.request_meta_data["meta_data"]["simulation_name"] = "crane_simulation"
        self.request_meta_data["send"][body_name] = ["position", "quaternion"]
        self._communicate(True)
        self.send_data = [time(), *pose.position_as_list(), *pose.orientation_as_list()]
        self._communicate(False)

    def disconnect_from_physics_server(self) -> None:
        self.stop()

    def join_threads(self) -> None:
        pass

    def remove_object_from_simulator(self, obj: Object) -> None:
        logging.warning("remove_object_from_simulator is not implemented in Multiverse")

    def add_constraint(self, constraint: Constraint) -> int:
        logging.warning("add_constraint is not implemented in Multiverse")
        return 0

    def remove_constraint(self, constraint_id) -> None:
        logging.warning("remove_constraint is not implemented in Multiverse")

    def perform_collision_detection(self) -> None:
        logging.warning("perform_collision_detection is not implemented in Multiverse")

    def get_object_contact_points(self, obj: Object) -> List:
        logging.warning("get_object_contact_points is not implemented in Multiverse")
        return []

    def get_contact_points_between_two_objects(self, obj1: Object, obj2: Object) -> List:
        logging.warning("get_contact_points_between_two_objects is not implemented in Multiverse")
        return []

    def ray_test(self, from_position: List[float], to_position: List[float]) -> int:
        logging.error("ray_test is not implemented in Multiverse")
        raise NotImplementedError

    def ray_test_batch(self, from_positions: List[List[float]], to_positions: List[List[float]],
                       num_threads: int = 1) -> List[int]:
        logging.error("ray_test_batch is not implemented in Multiverse")
        raise NotImplementedError

    def step(self):
        logging.warning("step is not implemented in Multiverse")

    def save_physics_simulator_state(self) -> int:
        logging.warning("save_physics_simulator_state is not implemented in Multiverse")
        return 0

    def remove_physics_simulator_state(self, state_id: int) -> None:
        logging.warning("remove_physics_simulator_state is not implemented in Multiverse")

    def restore_physics_simulator_state(self, state_id: int) -> None:
        logging.error("restore_physics_simulator_state is not implemented in Multiverse")
        raise NotImplementedError

    def set_link_color(self, link: Link, rgba_color: Color):
        logging.warning("set_link_color is not implemented in Multiverse")

    def get_link_color(self, link: Link) -> Color:
        logging.warning("get_link_color is not implemented in Multiverse")
        return Color()

    def get_colors_of_object_links(self, obj: Object) -> Dict[str, Color]:
        logging.warning("get_colors_of_object_links is not implemented in Multiverse")
        return {}

    def get_object_axis_aligned_bounding_box(self, obj: Object) -> AxisAlignedBoundingBox:
        logging.error("get_object_axis_aligned_bounding_box is not implemented in Multiverse")
        raise NotImplementedError

    def get_link_axis_aligned_bounding_box(self, link: Link) -> AxisAlignedBoundingBox:
        logging.error("get_link_axis_aligned_bounding_box is not implemented in Multiverse")
        raise NotImplementedError

    def set_realtime(self, real_time: bool) -> None:
        logging.warning("set_realtime is not implemented in Multiverse")

    def set_gravity(self, gravity_vector: List[float]) -> None:
        logging.warning("set_gravity is not implemented in Multiverse")
