## What is the Agent ?
It is a simple tool that enables you to remotely manage a large number of hosts.  It brings
together features of Configuration Management and Orchestration in a
lightweight framework.  Written in Python and using AMQP for messaging between
the nodes.

[View first screencast](http://www.youtube.com/watch?v=SrXDTZJLeGg)

## What do you need ?
* A RabbitMQ server
* The agent on the hosts you want to manage
* The client to control them

## Features
### REST API
Manipulates resources (packages, files, services, ...) instead of executing commands.
Describe a resource state and the tool takes care of it.
For example, installing a package means sending this kind of message:

```bash
{
    "collection": "packages",
    "action": "update",
    "id": "htop",
    "attributes": {
        "installed": true
    }
}
```

We also provide a client tool to ease its use or you can use http://comodit.com which alos benefit of the agents.


### OS Abstraction
Abstract the definition of resources so you can focus on the what, not the how.

The example above about how to install a package works independently of the underlying platform.

### Pluggable engine
Manage any kind of resources on the remote hosts with custom plugins. Available built-in resources are:
* files
* packages
* services
* users
* groups

Why not building a "cron" plugin.

How ? Take a look at example [packages-plugin](https://github.com/comodit/synapse-agent/tree/master/synapse/resources/packages-plugin) and start your own ?

### Lightweight
Easy to deploy, small memory footprint. Cehck our WIKI for our quickstart tour.

### Secure
No incoming open port required (not even port 22/ssh), secured with PKI/SSL

### Fast 
Parallelize tasks accross all your hosts.

The agents are automatically bound to an AMQP fanout exchange. It means that if a
single message is published into that exchange, the RabbitMQ server broadcasts
this message to every agent bound to that exchange.

### Flexible 
Reach nodes without knowing their ip or hostname, filter requests by hostname, ip, platform
Want to reach only hosts in particular subdomains ?
