output "vpc_id" {
  value = digitalocean_vpc.ctfvm.id
}

output "droplet_tags" {
  value = local.droplet_tags
}

output "firewall_id" {
  value = digitalocean_firewall.ctfvm.id
}

output "env_snippet" {
  value = join(
    "\n",
    [
      "CTFVM_DO_VPC_ID=${digitalocean_vpc.ctfvm.id}",
      "CTFVM_DO_TAGS=${join(",", local.droplet_tags)}",
      "CTFVM_CONTROL_PORT=${var.control_plane_port}",
    ],
  )
}
