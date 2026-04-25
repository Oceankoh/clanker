locals {
  region = var.region != "" ? var.region : join("-", slice(split("-", var.zone), 0, 2))
}

resource "google_compute_network" "ctfvm" {
  name                    = "${var.name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "GLOBAL"
}

resource "google_compute_subnetwork" "ctfvm" {
  name                     = "${var.name_prefix}-subnet"
  region                   = local.region
  network                  = google_compute_network.ctfvm.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

resource "google_compute_firewall" "control_plane" {
  name          = "${var.name_prefix}-control-plane"
  network       = google_compute_network.ctfvm.name
  direction     = "INGRESS"
  source_ranges = var.allowed_cidrs
  target_tags   = [var.control_plane_tag]

  allow {
    protocol = "tcp"
    ports    = [tostring(var.control_plane_port)]
  }
}

resource "google_compute_firewall" "ssh" {
  count         = var.enable_ssh ? 1 : 0
  name          = "${var.name_prefix}-ssh"
  network       = google_compute_network.ctfvm.name
  direction     = "INGRESS"
  source_ranges = var.allowed_cidrs
  target_tags   = [var.control_plane_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}
