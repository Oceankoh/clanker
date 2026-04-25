output "network_name" {
  value = google_compute_network.ctfvm.name
}

output "subnet_name" {
  value = google_compute_subnetwork.ctfvm.name
}

output "region" {
  value = local.region
}

output "control_plane_tag" {
  value = var.control_plane_tag
}

output "env_snippet" {
  value = join(
    "\n",
    [
      "CTFVM_GCP_NETWORK=${google_compute_network.ctfvm.name}",
      "CTFVM_GCP_SUBNET=${google_compute_subnetwork.ctfvm.name}",
      "CTFVM_GCP_NETWORK_TAGS=${var.control_plane_tag}",
      "CTFVM_CONTROL_PORT=${var.control_plane_port}",
    ],
  )
}
