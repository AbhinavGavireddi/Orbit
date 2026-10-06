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
  tags = ["orbit-task:prototype"]
}
target "voice" {
  inherits = ["base"]
  args = { SERVICE = "voice" }
  tags = ["orbit-voice:prototype"]
}
target "automation" {
  inherits = ["base"]
  args = { SERVICE = "automation" }
  tags = ["orbit-automation:prototype"]
}
target "research" {
  inherits = ["base"]
  args = { SERVICE = "research" }
  tags = ["orbit-research:prototype"]
}
target "decision" {
  inherits = ["base"]
  args = { SERVICE = "decision" }
  tags = ["orbit-decision:prototype"]
}
target "room" {
  inherits = ["base"]
  args = { SERVICE = "room" }
  tags = ["orbit-room:prototype"]
}
