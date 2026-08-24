# Physical adapters

Future vendor integrations implement `autonomous_lab.hardware.interfaces.Device`
and publish an exact `CapabilityManifest`. Adapters translate typed AutoLab
commands to a vendor API; they do not compile protocols or accept optimiser or
research-agent input.
