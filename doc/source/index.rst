==========================
Config Template collection
==========================

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
The `config_template` collection has a variety of tools available. A .conf file
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
            option2 = value2
          dest: "test_dst.conf"
          config_type: "ini"
          config_overrides:
            hello:
              cruel: world
            section1:
              option1: value2
              option2: {}
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

Take notice of the `option2` in `section1`, which is removed in the output but
requires an empty dictionary in the playbook YAML. The empty dictionary is
translated to `None` value and the default behavior is to remove keys with
`None` values. See parameter `ignore_none_type`, which defaults to `true`.

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


Preventing content from renderring
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

There are few different way that can be used to prevent some content
from being renderred. First one is to use Jinja's ``{% raw %}`` tag.

Template:

.. code-block :: ini

  [foo]
  # comment
  bar = {% raw %}{{ baz }}{% endraw %}


Result:

.. code-block :: ini

  [foo]
  # comment
  bar = {{ baz }}

Another way around could be customizing Jinja tags used to identify variables
and block string. For that `variable_start/end_string` or `block_start/end_string`
options could be used. These variables could be provided as an arguments to
the module or by adding a special header to template file.

.. Note::

  Please mention, that changing identification for start/end of blocks works only
  inside the template and does not affect ``config_overrides`` option.

Template:

.. code-block :: ini


  #jinja2:variable_start_string:'[%', variable_end_string:'%]'

  [foo]
  # comment
  bar = {{ baz }}
  foo = [% inventory_hostname %]


Result:

.. code-block :: ini

  [foo]
  # comment
  bar = {{ baz }}
  foo = localhost


---------------------

To use the collection, include this in your meta/main.yml:

.. code-block :: yaml

   collections:
     - openstack.config_template



Also the Ansible requirement file can be used with the
``ansible-galaxy`` command to automatically fetch the collections for
you in a given project. To do this add the following lines to your
``requirements.yml`` file.

.. literalinclude:: ../../examples/ansible-role-requirements.yml
   :language: yaml

After that simple run the following command to get requirements installed:

  .. code-block :: shell

    $ ansible-galaxy install -r requirements.yml
