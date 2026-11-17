output "instance_ids" {
  value = { for k, i in aws_instance.lab : k => i.id }
}
