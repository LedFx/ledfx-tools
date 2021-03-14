#!/usr/bin/env bash
# shellcheck disable=SC1090
if [ -t 0 ]; then
  screen_size=$(stty size)
else
  screen_size="24 80"
fi
# Set rows variable to contain first number
printf -v rows '%d' "${screen_size%% *}"
# Set columns variable to contain second number
printf -v columns '%d' "${screen_size##* }"

# Divide by two so the dialogs take up half of the screen, which looks nice.
r=$((rows / 2))
c=$((columns / 2))
# Unless the screen is tiny
r=$((r < 20 ? 20 : r))
c=$((c < 70 ? 70 : c))
# Pretty logo so we can verify it's us.
curl -sSL https://install.ledfx.app/ledfxrainbow.out | cat
# Could probably do something more productive here to display the logo, but oh well.
sleep 3

install_python39() {
  FILE="$(which python3.9)"
  installed_39="false"
  python3_version="$(python3 -V 2>&1)"
  if [ -f "$FILE" ]; then
  python39_version="$("$FILE" -V 2>&1)"
  installed_39="true"
  
  fi

  if [ "$python3_version" = "Python 3.9.0" ]; then
    echo "Python 3.9.0 Already Installed"
    installed_39="true"
    menu
  fi

  if [ "$python3_version" = "Python 3.9.1" ]; then
    echo "Python 3.9.1 Already Installed"
    installed_39="true"
    menu
  fi

  if [ "$python39_version" = "Python 3.9.2" ]; then
    echo "Python 3.9.2 Already Installed"
    installed_39="true"
    menu
  fi

  
  if [ "$installed_39" = "false" ]; then
    whiptail --yesno "LedFx requires Python 3.9 or greater. Would you like to install Python 3.9 now?" --yes-button "Yes" --no-button "No" "${r}" "${c}"
    INST_PYTHON=$?
    if [ "$INST_PYTHON" = "0" ]; then
      echo "Ensuring build environment setup correctly for python 3.9 installation"
      sudo apt-get update -y
      sudo apt-get upgrade -y
      sudo apt-get install -y gcc \
      git \
      libatlas3-base \
      libavformat58 \
      portaudio19-dev \
      pulseaudio \
      avahi-daemon \
      build-essential \
      tk-dev \
      libncurses5-dev \
      libncursesw5-dev \
      libreadline6-dev \
      libdb5.3-dev \
      libgdbm-dev \
      libsqlite3-dev \
      libssl-dev \
      libbz2-dev \
      libexpat1-dev \
      liblzma-dev \
      zlib1g-dev \
      libffi-dev \
      libtiff-dev \
      autoconf \
      libopenjp2-7

      # Python3.9 build from source

      version=3.9.2
      wget -O /tmp/Python-$version.tar.xz https://www.python.org/ftp/python/$version/Python-$version.tar.xz
      cd /tmp/ || exit
      tar xf Python-$version.tar.xz
      rm Python-$version.tar.xz 
      cd Python-$version/ || exit
      ./configure --enable-optimizations
      sudo make altinstall
      sudo apt -y autoremove
      cd ~ || exit
      rm -rf Python-$version/
      export C_INCLUDE_PATH=/usr/local/include/python3.9:/usr/lib/python3/dist-packages/numpy/core/include/numpy/
      export CPLUS_INCLUDE_PATH=/usr/local/include/python3.9:/usr/lib/python3/dist-packages/numpy/core/include/numpy/
      sudo ln -s /usr/local/bin/python3.9 /usr/bin/python3.9
      echo "alias python3.9=/usr/local/bin/python3.9" >>~/.bashrc
      # End 3.9 source build
      menu
    fi
  fi
}

install-ledfx() {
  echo "Ensuring  build environment setup correctly for LedFx installation"
  sudo apt-get update
  sudo apt-get install -y gcc \
  git \
  libatlas3-base \
  libavformat58 \
  portaudio19-dev \
  pulseaudio \
  avahi-daemon \
  llvm-9
  sudo ln -s /usr/bin/llvm-config-9 /usr/bin/llvm-config
  python3.9 -m venv ~/.ledfx/ledfx-venv
  source ~/.ledfx/ledfx-venv/bin/activate
  python3.9 -m pip install --upgrade pip wheel setuptools aubio
  curruser=$USER
  IP=$(/sbin/ip -o route get to 8.8.8.8 | sed -n 's/.*src \([0-9.]\+\).*/\1/p')
  echo "Downloading and installing latest version of LedFx from github"
  python3.9 -m pip install --no-cache-dir git+https://github.com/LedFx/LedFx@dev
  echo "Adding" $curruser "to Audio Group"
  sudo usermod -a -G audio $curruser
  echo "alias ledfx='~/.ledfx/ledfx-venv/bin/python3.9 ~/.ledfx/ledfx-venv/bin/ledfx'" >>~/.bashrc
  whiptail --yesno "Install LedFx as a service so it launches automatically on boot?" --yes-button "Yes" --no-button "No" "${r}" "${c}"
  SERVICE=$?
  if [ "$SERVICE" = "0" ]; then

    echo "Installing LedFx Service"
    echo "[Unit]
    Description=LedFx Music Visualizer
    After=network.target sound.target
    StartLimitIntervalSec=0

    [Service]
    Type=simple
    Restart=always
    RestartSec=5
    User="$curruser"
    Group=audio
    ExecStart=/home/"$curruser"/.ledfx/ledfx-venv/bin/python3.9 /home/"$curruser"/.ledfx/ledfx-venv/bin/ledfx
    Environment=XDG_RUNTIME_DIR=/run/user/"$UID"
    [Install]
    WantedBy=multi-user.target
    " >>~/ledfx.service
    sudo mv ~/ledfx.service /etc/systemd/system/ledfx.service
    sudo systemctl enable ledfx
    sudo systemctl start ledfx
    echo "LedFx is now running. Please navigate to "$IP":8888 in your web browser"
    echo "If you have no audio devices in LedFx and you're on a Raspberry Pi, please run 'sudo raspi-config' and setup your audio device (System Devices -> Audio)"

  else

    echo "LedFx is now installed. Please type ledfx to start."
    echo "If you have no audio devices in LedFx and you're on a Raspberry Pi, please run 'sudo raspi-config' and setup your audio device (System Devices -> Audio)"
  fi
}

update-ledfx() {
  source ~/.ledfx/ledfx-venv/bin/activate
  sudo systemctl stop ledfx 2>/dev/null
  python3.9 -m pip install --no-cache-dir --upgrade --force-reinstall git+https://github.com/LedFx/LedFx@dev
  echo "All Updated, enjoy LedFx!"
  sudo systemctl start ledfx 2>/dev/null
}

delete-config() {
  source ~/.ledfx/ledfx-venv/bin/activate
  sudo systemctl stop ledfx 2>/dev/null
  echo "Stopping Service..."
  sleep 2
  rm ~/.ledfx/config.json
  echo "Configuration Deleted"
  echo "Restarting Service..."
  sudo systemctl start ledfx 2>/dev/null
  echo "Relaunch LedFx to rebuild if you aren't using a service. Otherwise you're good to go."
}

backup-config() {
  cp ~/.ledfx/config.json ~/ledfx_config.json.bak
  menu
}

uninstall-ledfx() {
  source ~/.ledfx/ledfx-venv/bin/activate
  echo "Removing LedFx installation and configuration"
  sudo systemctl stop ledfx 2>/dev/null
  sudo systemctl disable ledfx 2>/dev/null
  sudo rm /etc/systemd/system/ledfx.service 2>/dev/null
  python3.9 -m pip -q uninstall -y ledfx 2>/dev/null
  unalias ledfx
  deactivate
  rm -rf ~/.ledfx/
  echo "LedFx uninstalled. Sorry to see you go :("
}

repair-ledfx() {
  source ~/.ledfx/ledfx-venv/bin/activate
  echo "Removing old LedFx installation"
  sudo systemctl stop ledfx 2>/dev/null
  sudo systemctl disable ledfx 2>/dev/null
  sudo rm /etc/systemd/system/ledfx.service 2>/dev/null
  python3.9 -m pip -q uninstall -y ledfx 2>/dev/null
  install-ledfx
}

menu() {
  FILE=~/.ledfx/ledfx-venv/bin/ledfx
  source ~/.ledfx/ledfx-venv/bin/activate
  if [ -f "$FILE" ]; then

    INSTALLOPTION=$(
      whiptail --title "LedFx Installer" --menu "Prior Installation Detected" "${r}" "${c}" 14 \
      "Update" "Update LedFx." \
      "Fresh Install" "Remove all data (INCLUDING CONFIGURATION) and reinstall." \
      "Uninstall" "Removes LedFx." \
      "Repair" "Attempts to repair LedFx installation."\
      "Backup Config" "Backs up your configuration file to your home folder." \
      "Delete Config" "Sometimes your configuration file can cause issues." 3>&2 2>&1 1>&3
    )

    if [ "$INSTALLOPTION" = "Update" ]; then
      update-ledfx
    elif [ "$INSTALLOPTION" = "Fresh Install" ]; then
      install-ledfx
    elif [ "$INSTALLOPTION" = "Uninstall" ]; then
      uninstall-ledfx
    elif [ "$INSTALLOPTION" = "Repair" ]; then
      repair-ledfx
    elif [ "$INSTALLOPTION" = "Backup Config" ]; then
      backup-config
    elif [ "$INSTALLOPTION" = "Delete Config" ]; then
      delete-config
    else
      echo "What happened? We broke? Give me another go!"
    fi

  else
    install-ledfx
  fi
}
install_python39
