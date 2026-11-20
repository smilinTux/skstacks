mock_provider "libvirt" {}

variables {
  cluster_name   = "t06"
  env            = "dev"
  network_cidr   = "10.99.0.0/24"
  ssh_public_key = "ssh-ed25519 AAAA test"
}

run "apt_proxy_is_opt_in" {
  command = plan
  assert {
    condition     = !strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "\napt:")
    error_message = "apt: key must not render by default"
  }
  assert {
    condition     = !strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "http_proxy")
    error_message = "no proxy key may render by default"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "ssh_pwauth: false\npackage_update: true")
    error_message = "default rendering shape changed: apt_proxy unset must render byte-identical cloud-init"
  }
}

run "apt_proxy_set_renders_both_keys" {
  command = plan
  variables {
    apt_proxy = "http://apt-cache.example:3142"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "ssh_pwauth: false\n# Only http sources")
    error_message = "apt block must sit directly after ssh_pwauth"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "\napt:\n  preserve_sources_list: true\n  http_proxy: \"http://apt-cache.example:3142\"\npackage_update: true")
    error_message = "apt http_proxy key missing or malformed"
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-wkr-1"].user_data, "http_proxy: \"http://apt-cache.example:3142\"")
    error_message = "proxy must reach every node, workers included"
  }
  assert {
    condition     = !strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "https_proxy")
    error_message = "no https proxy: the https Docker repo must be fetched direct"
  }
}

run "apt_proxy_with_write_files_stays_valid" {
  command = plan
  variables {
    apt_proxy            = "http://apt-cache.example:3142"
    disable_auto_updates = true
    registry_mirrors     = ["https://mirror.example.test"]
  }
  assert {
    condition     = strcontains(libvirt_cloudinit_disk.node["t06-mgr-1"].user_data, "http_proxy: \"http://apt-cache.example:3142\"\nwrite_files:\n  - path: /etc/docker/daemon.json")
    error_message = "apt: block must not collide with write_files"
  }
}

run "apt_proxy_rejects_https" {
  command = plan
  variables {
    apt_proxy = "https://apt-cache.example:3142"
  }
  expect_failures = [var.apt_proxy]
}

run "apt_proxy_rejects_bare_host" {
  command = plan
  variables {
    apt_proxy = "apt-cache.example:3142"
  }
  expect_failures = [var.apt_proxy]
}

run "apt_proxy_rejects_credentials_and_quotes" {
  command = plan
  variables {
    apt_proxy = "http://user:pw@apt-cache.example:3142\""
  }
  expect_failures = [var.apt_proxy]
}
