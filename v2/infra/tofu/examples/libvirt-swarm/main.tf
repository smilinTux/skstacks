# SKStacks v2 example: local libvirt/KVM VMs + Docker Swarm.
#
#   cd v2/infra/tofu/examples/libvirt-swarm
#   cp terraform.tfvars.example terraform.tfvars && $EDITOR terraform.tfvars
#   tofu init && tofu apply
#   tofu output -raw ansible_inventory > inventory.ini
#   ansible-playbook -i inventory.ini ../../../../platform/swarm/ansible/playbooks/deploy.yml
#
# An instance repo calls the module the same way, with source pointing into its
# framework/ submodule.

variable "cluster_name" { type = string }
variable "network_cidr" { type = string }
variable "ssh_public_key_file" { type = string }

module "cluster" {
  source         = "../../modules/libvirt-cluster"
  cluster_name   = var.cluster_name
  env            = "dev"
  network_cidr   = var.network_cidr
  ssh_public_key = trimspace(file(pathexpand(var.ssh_public_key_file)))
}

output "ansible_inventory" {
  value = module.cluster.ansible_inventory
}
