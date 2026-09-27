mock_provider "libvirt" {}

variables {
  cluster_name   = "t06"
  env            = "dev"
  network_cidr   = "10.99.0.0/24"
  ssh_public_key = "ssh-ed25519 AAAA test"
}

run "auto_updates_disabling_is_opt_in" {
  command = plan
  assert {
    condition     = !strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "skstacks-no-periodic")
    error_message = "apt periodic overrides must not render by default"
  }
  assert {
    condition     = !strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "apt-daily.timer")
    error_message = "apt-daily.timer mask must not render by default"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "ssh_pwauth: false\npackage_update: true")
    error_message = "default rendering shape changed: write_files block must stay absent with no daemon_json and disable_auto_updates=false"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "runcmd:\n  - install -m 0755 -d /etc/apt/keyrings")
    error_message = "default rendering shape changed: runcmd must still start directly with the docker install step"
  }
}

run "disable_auto_updates_neutralizes_background_update_paths" {
  command = plan
  variables {
    disable_auto_updates = true
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "/etc/apt/apt.conf.d/99-skstacks-no-periodic")
    error_message = "apt periodic update/download/unattended-upgrade config missing"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "systemctl mask apt-daily.timer apt-daily-upgrade.timer apt-daily.service apt-daily-upgrade.service unattended-upgrades.service")
    error_message = "apt-daily/unattended-upgrades units not masked"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "Unattended-Upgrade::Automatic-Reboot \"false\"")
    error_message = "automatic reboot not disabled"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "$nrconf{restart} = 'l';")
    error_message = "needrestart not set to list-only (no auto-restart)"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "motd-news.timer")
    error_message = "motd-news not disabled"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "snap refresh --hold")
    error_message = "snapd auto-refresh not held"
  }
}
