locals {
  droplet_tags = [var.fleet_tag, var.control_plane_tag]
}

resource "digitalocean_vpc" "ctfvm" {
  name     = "${var.name_prefix}-vpc"
  region   = var.region
  ip_range = var.ip_range
}

resource "digitalocean_tag" "fleet" {
  name = var.fleet_tag
}

resource "digitalocean_tag" "control_plane" {
  name = var.control_plane_tag
}

resource "digitalocean_firewall" "ctfvm" {
  name = "${var.name_prefix}-firewall"
  tags = local.droplet_tags

  inbound_rule {
    protocol         = "tcp"
    port_range       = tostring(var.control_plane_port)
    source_addresses = var.allowed_cidrs
  }

  dynamic "inbound_rule" {
    for_each = var.enable_ssh ? [1] : []
    content {
      protocol         = "tcp"
      port_range       = "22"
      source_addresses = var.allowed_cidrs
    }
  }

  outbound_rule {
    protocol              = "tcp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "udp"
    port_range            = "1-65535"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }

  outbound_rule {
    protocol              = "icmp"
    destination_addresses = ["0.0.0.0/0", "::/0"]
  }
}
