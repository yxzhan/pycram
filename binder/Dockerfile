FROM intel4coro/base-notebook:20.04-noetic-vnc

USER root
# Upgrade CMAKE
RUN apt update && apt install -y build-essential software-properties-common
RUN wget -O - https://apt.kitware.com/keys/kitware-archive-latest.asc 2>/dev/null | gpg --dearmor - | sudo tee /etc/apt/trusted.gpg.d/kitware.gpg >/dev/null
RUN apt-add-repository "deb https://apt.kitware.com/ubuntu/ $(lsb_release -cs) main"
RUN apt update && apt install -y cmake

# install robotology-superbuild 
ARG SOURCE_FOLDER=/usr/local/src/robot
RUN mkdir -p $SOURCE_FOLDER && \
    chown -R $NB_USER $SOURCE_FOLDER

USER ${NB_USER}
WORKDIR $SOURCE_FOLDER
RUN git clone https://github.com/robotology/robotology-superbuild.git
WORKDIR $SOURCE_FOLDER/robotology-superbuild
RUN git checkout f9c6fa2
RUN git config --local user.name "oeldardear"
RUN git config --local user.email "dardearynote@gmail.com"

USER root
RUN bash ./scripts/install_apt_dependencies.sh 

USER ${NB_USER}
RUN mkdir build
WORKDIR $SOURCE_FOLDER/robotology-superbuild/build
RUN cmake .. -DROBOTOLOGY_USES_PYTHON=ON -DROBOTOLOGY_USES_GAZEBO=ON 
RUN make

ARG CACHEBUST=4

ENV AMENT_PREFIX_PATH=$AMENT_PREFIX_PATH:/usr/local/src/robot/robotology-superbuild/build/install

RUN git clone https://gitlab.iit.it/cognitiveInteraction/icub-pycram-docker.git /tmp/icub-pycram-docker && \
    cp -r /tmp/icub-pycram-docker/scripts /usr/local/src/robot/scripts

RUN chmod +x /usr/local/src/robot/scripts/kill_icub.sh
RUN chmod +x /usr/local/src/robot/scripts/run_icub.sh

# installing pycram
RUN mkdir -p $SOURCE_FOLDER/catkin_ws/src
WORKDIR $SOURCE_FOLDER/catkin_ws/src
RUN vcs import --input https://raw.githubusercontent.com/cram2/pycram/dev/pycram-https.rosinstall --recursive
RUN rm -rf $SOURCE_FOLDER/catkin_ws/src/pycram
COPY --chown=${NB_USER}:users . $SOURCE_FOLDER/catkin_ws/src/pycram
RUN cp -r /tmp/icub-pycram-docker/icub_model $SOURCE_FOLDER/catkin_ws/src/icub_model
WORKDIR $SOURCE_FOLDER/catkin_ws/src/pycram
RUN git submodule init
RUN git submodule update
RUN pip install -r requirements.txt

USER root
WORKDIR $SOURCE_FOLDER/catkin_ws
RUN rosdep update
RUN rosdep install -y --ignore-src --from-paths . -r

USER ${NB_USER}
WORKDIR $SOURCE_FOLDER/catkin_ws
RUN /bin/bash -c "source /usr/local/src/robot/robotology-superbuild/build/install/share/robotology-superbuild/setup.sh &&\
       source /opt/ros/noetic/setup.bash && catkin build"

RUN echo 'source /usr/local/src/robot/robotology-superbuild/build/install/share/robotology-superbuild/setup.sh' >> ~/.bashrc
RUN echo 'source /opt/ros/noetic/setup.bash' >> ~/.bashrc
RUN echo 'export YARP_ROBOT_NAME="icub"' >> ~/.bashrc
RUN echo "source /usr/share/gazebo/setup.bash" >> ~/.bashrc

RUN echo 'alias runIcub="source /usr/local/src/robot/scripts/run_icub.sh"' >> ~/.bashrc
RUN echo 'alias killIcub="source /usr/local/src/robot/scripts/kill_icub.sh"' >> ~/.bashrc
RUN echo 'export PYTHONPATH=$PYTHONPATH:/usr/local/src/robot/catkin_ws/src/pycram/src' >> ~/.bashrc

WORKDIR $SOURCE_FOLDER
COPY --chown=${NB_USER}:users binder/entrypoint.sh /
ENTRYPOINT [ "/entrypoint.sh" ]
