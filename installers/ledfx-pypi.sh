  #!/usr/bin/env bash
  # shellcheck disable=SC1090
  if [ -t 0 ] ; then
    screen_size=$(stty size)
  else
    screen_size="24 80"
  fi
  # Set rows variable to contain first number
  printf -v rows '%d' "${screen_size%% *}"
  # Set columns variable to contain second number
  printf -v columns '%d' "${screen_size##* }"

  # Divide by two so the dialogs take up half of the screen, which looks nice.
  r=$(( rows / 2 ))
  c=$(( columns / 2 ))
  # Unless the screen is tiny
  r=$(( r < 20 ? 20 : r ))
  c=$(( c < 70 ? 70 : c ))

  install-ledfx () {
    echo "Ensuring build environment setup correctly"
    sudo apt-get update
    sudo apt-get install -y gcc \
            git \
            libatlas3-base \
            libavformat58 \
            portaudio19-dev \
            pulseaudio \
            python3-pip \
            avahi-daemon
    python3 -m pip install --upgrade pip wheel setuptools pipx
    curruser=$USER
    IP=$(/sbin/ip -o route get to 8.8.8.8 | sed -n 's/.*src \([0-9.]\+\).*/\1/p')
    echo "Downloading and installing latest version of LedFx from github"
    python3 -m pipx install ledfx
    echo "Adding" $curruser "to Audio Group"
    sudo usermod -a -G audio $curruser
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
    ExecStart=/usr/bin/python3 /home/"$curruser"/.local/bin/ledfx
    Environment=XDG_RUNTIME_DIR=/run/user/"$UID"
    [Install]
    WantedBy=multi-user.target
    " >> ~/ledfx.service
    sudo mv ~/ledfx.service /etc/systemd/system/ledfx.service
    sudo systemctl enable ledfx
    sudo systemctl start ledfx
    sudo systemctl status ledfx
    echo "LedFx is now running. Please navigate to "$IP":8888 in your web browser"
    echo "If you have no audio devices in LedFx and you're on a Raspberry Pi, please run 'sudo raspi-config' and setup your audio device (System Devices -> Audio)"

    else

    echo "LedFx is now installed. Please type ledfx to start."
    echo "If you have no audio devices in LedFx and you're on a Raspberry Pi, please run 'sudo raspi-config' and setup your audio device (System Devices -> Audio)"
    fi
  }

  update-ledfx () {
    sudo systemctl stop ledfx 2> /dev/null
    python3 -m pipx install --upgrade --force-reinstall --no-deps --no-cache-dir ledfx
    echo "All Updated, enjoy LedFx!"
    sudo systemctl start ledfx 2> /dev/null
  }

  delete-config () {
    sudo systemctl stop ledfx 2> /dev/null
    echo "Stopping Service..."
    sleep 2
    rm ~/.ledfx/config.json
    echo "Configuration Deleted"
    echo "Restarting Service..."
    sudo systemctl start ledfx 2> /dev/null
    echo "Relaunch LedFx to rebuild if you aren't using a service. Otherwise you're good to go."
  }

  backup-config (){
    cp ~/.ledfx/config.json ~/config.json.bak
    menu
  }

  uninstall-ledfx () {
    echo "Removing LedFx installation and configuration"
    sudo systemctl stop ledfx 2> /dev/null
    sudo systemctl disable ledfx 2> /dev/null
    sudo rm /etc/systemd/system/ledfx.service 2> /dev/null
    python3 -m pipx -q uninstall -y ledfx 2> /dev/null


    rm -rf ~/.ledfx/
    echo "LedFx uninstalled. Sorry to see you go :("
  }

  repair-ledfx () {
    echo "Removing old LedFx installation"
    sudo systemctl stop ledfx 2> /dev/null
    sudo systemctl disable ledfx 2> /dev/null
    sudo rm /etc/systemd/system/ledfx.service 2> /dev/null
    python3 -m pipx -q uninstall -y ledfx 2> /dev/null
    install-ledfx
  }

  menu () {
    FILE=~/.ledfx/config.json
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
  menu
