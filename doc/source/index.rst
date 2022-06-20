======================
Config Template plugin
======================

Synopsis
--------
Renders template files providing a create/update override interface

- The module contains the template functionality with the ability to override
  items in config, in transit, through the use of a simple dictionary without
  having to write out various temp files on target machines. The module renders
  all of the potential jinja a user could provide in both the template file and
  in the override dictionary which is ideal for deployers who may have lots of
  different configs using a similar code base.
- The module is an extension of the **copy** module and all of attributes that
  can be set there are available to be set here.

Examples
--------

Example for .conf
^^^^^^^^^^^^^^^^^
The `config_template` plugin has a variety of tools available. A .conf file
may include options that are not normally supported in an INI file, but are
used in OpenStack, like ListOpt and MultiStrOpt.

Even though we are generating a .conf file, we specify the `config_type`` of
`ini`` when using config_template.

Playbook:

.. code-block :: yaml

  - hosts: localhost
    connection: local
    gather_facts: no
    tasks:
      - openstack.config_template.config_template:
          content: |
            [foo]
            bar = baz

            [section1]
            option1 = value1
          dest: "test_dst.conf"
          config_type: "ini"
          config_overrides:
            hello:
              cruel: world
            section1:
              option1: value2
            orderedListSection:
              listOpt:
                - listItem1
                - listItem2
            multiStrOpSection:
              multiStrOpOption:
                ? multiStrOp1
                ? multiStrOp2

Resulting file on the remote host:

.. code-block :: ini

  [foo]
  bar = baz

  [section1]
  option1 = value2

  [hello]
  cruel = world

  [orderedListSection]
  listOpt = listItem1,listItem2

  [multiStrOpSection]
  multiStrOpOption = multiStrOp1
  multiStrOpOption = multiStrOp2

A practical example would be for something like OpenStack's nova.conf where the
input of:

.. code-block :: yaml

  nova_conf_override:
    filter_scheduler:
      enabled_filters:
        - ComputeFilter
        - NUMATopologyFilter
    pci:
      ? passthrough_whitelist: '{"address":"*:0a:00.*",
        "physical_network":"pn1"}'
      ? passthrough_whitelist: '{"vendor_id":"1137","product_id":"0071"}'

Would produce:

.. code-block :: ini

  [filter_scheduler]
  enabled_filters = ComputeFilter,NUMATopologyFilter

  [pci]
  passthrough_whitelist = '{"address":"*:0a:00.*", "physical_network":"pn1"}'
  passthrough_whitelist = '{"vendor_id":"1137","product_id":"0071"}'

Example for .ini with remote_src
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
The `remote_src: true` argument instructs `config_template` to use a file that
is already on the remote host as the source content.

Input file on the remote host:

.. code-block :: ini

  [foo]
  # comment
  bar = baz

  [hello]

Playbook:

.. code-block :: yaml

  - hosts: remote_host
    gather_facts: no
    tasks:
      - config_template:
          remote_src: true
          src: "/etc/test_src.ini"
          dest: "/etc/test_dst.ini"
          config_type: "ini"
          config_overrides:
            hello:
              cruel: world

Resulting file on the remote host:

.. code-block :: ini

  [foo]
  # comment
  bar = baz

  [hello]
  cruel = world

Loading
-------

To use the plugin, include this role in your meta/main.yml dependencies

.. code-block :: yaml

   dependencies:
     - role: ansible-config_template

Alternatively, move the role to the appropriate plugin folder location
of your ansible configuration.

Example role requirement overload for automatic plugin download
---------------------------------------------------------------

The Ansible role requirement file can be used to overload the
``ansible-galaxy`` command to automatically fetch the plugins for
you in a given project. To do this add the following lines to your
``ansible-role-requirements.yml`` file.

.. literalinclude:: ../../examples/ansible-role-requirements.yml
   :language: yaml
