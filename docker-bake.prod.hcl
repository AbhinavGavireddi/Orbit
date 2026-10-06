variable "REGISTRY" {
  default = "ghcr.io/your-org"
}

variable "VERSION" {
  default = "0.1.0"
}

group "default" {
  targets = ["task", "voice", "automation", "research", "decision", "room"]
}

target "base" {
  context = "."
  dockerfile = "Dockerfile"
  platforms = ["linux/arm64", "linux/amd64"]
}

target "task" {
  inherits = ["base"]
  args = { SERVICE = "task" }
  tags = ["${REGISTRY}/orbit-task:${VERSION}"]
}

target "voice" {
  inherits = ["base"]
  args = { SERVICE = "voice" }
  tags = ["${REGISTRY}/orbit-voice:${VERSION}"]
}

target "automation" {
  inherits = ["base"]
  args = { SERVICE = "automation" }
  tags = ["${REGISTRY}/orbit-automation:${VERSION}"]
}

target "research" {
  inherits = ["base"]
  args = { SERVICE = "research" }
  tags = ["${REGISTRY}/orbit-research:${VERSION}"]
}

target "decision" {
  inherits = ["base"]
  args = { SERVICE = "decision" }
  tags = ["${REGISTRY}/orbit-decision:${VERSION}"]
}
target "room" {
  inherits = ["base"]
  args = { SERVICE = "room" }
  tags = ["${REGISTRY}/orbit-room:${VERSION}"]
}
