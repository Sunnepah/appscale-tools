#!/usr/bin/env python
# Programmer: Chris Bunch (chris@appscale.com)


# First-party Python imports
import getpass
import hashlib
import json
import os
import re
import time
import uuid
import yaml


# Third-party imports
import M2Crypto


# AppScale-specific imports
from appcontroller_client import AppControllerClient
from appscale_logger import AppScaleLogger
from custom_exceptions import AppScaleException
from custom_exceptions import BadConfigurationException


# The version of the AppScale Tools we're running on.
APPSCALE_VERSION = "1.6.6"


class LocalState():
  """LocalState handles all interactions necessary to read and write AppScale
  configuration files on the machine that executes the AppScale Tools.
  """


  # The number of times to execute shell commands before aborting, by default.
  DEFAULT_NUM_RETRIES = 5


  # The path on the local filesystem where we can read and write
  # AppScale deployment metadata.
  LOCAL_APPSCALE_PATH = os.path.expanduser("~") + os.sep + ".appscale" + os.sep


  # The length of the randomly generated secret that is used to authenticate
  # AppScale services.
  SECRET_KEY_LENGTH = 32


  # The username for the cloud administrator if the --test options is used.
  DEFAULT_USER = "a@a.com"


  # The password to set for the default user.
  DEFAULT_PASSWORD = "aaaaaa"


  @classmethod
  def make_appscale_directory(cls):
    """Creates a ~/.appscale directory, if it doesn't already exist.
    """
    if os.path.exists(cls.LOCAL_APPSCALE_PATH):
      return
    else:
      os.mkdir(cls.LOCAL_APPSCALE_PATH)


  @classmethod
  def ensure_appscale_isnt_running(cls, keyname, force):
    """Checks the locations.yaml file to see if AppScale is running, and
    aborts if it is.

    Args:
      keyname: The keypair name that is used to identify AppScale deployments.
      force: A bool that is used to run AppScale even if the locations file
        is present.
    Raises:
      BadConfigurationException: If AppScale is already running.
    """
    if force:
      return

    if os.path.exists(cls.get_locations_yaml_location(keyname)):
      raise BadConfigurationException("AppScale is already running. Terminate" +
        " it or use the --force flag to run anyways.")


  @classmethod
  def generate_secret_key(cls, keyname):
    """Creates a new secret, which is used to authenticate callers that
    communicate between services in an AppScale deployment.

    Args:
      keyname: A str representing the SSH keypair name used for this AppScale
        deployment.
    Returns:
      A str that represents the secret key.
    """
    key = str(uuid.uuid4()).replace('-', '')[:cls.SECRET_KEY_LENGTH]
    with open(cls.get_secret_key_location(keyname), 'w') as file_handle:
      file_handle.write(key)
    return key


  @classmethod
  def get_secret_key_location(cls, keyname):
    """Returns the path on the local filesystem where the secret key can be
    located.

    Args:
      keyname: A str representing the SSH keypair name used for this AppScale
        deployment.
    Returns:
      A str that corresponds to a location on the local filesystem where the
      secret key can be found.
    """
    return cls.LOCAL_APPSCALE_PATH + keyname + ".secret"


  @classmethod
  def get_secret_key(cls, keyname):
    """Retrieves the secret key, used to authenticate AppScale services.

    Args:
      keyname: A str representing the SSH keypair name used for this AppScale
        deployment.
    Returns:
      A str containing the secret key.
    """
    with open(cls.get_secret_key_location(keyname), 'r') as file_handle:
      return file_handle.read()


  @classmethod
  def write_key_file(cls, location, contents):
    """Writes the SSH key contents to the given location and makes it
    usable for SSH connections.

    Args:
      location: A str representing the path on the local filesystem where the
        SSH key should be written to.
      contents: A str containing the SSH key.
    """
    with open(location, 'w') as file_handle:
      file_handle.write(contents)
    os.chmod(location, 0600)  # so that SSH will accept the key


  @classmethod
  def generate_deployment_params(cls, options, node_layout, first_host,
    additional_creds):
    """Constructs a dict that tells the AppController which machines are part of
    this AppScale deployment, what their roles are, and how to host API services
    within this deployment.

    Args:
      options: A Namespace that dictates API service information, not including
        information about machine to role hosting.
      node_layout: A NodeLayout that indicates which machines host which roles
        (API services).
      first_host: A str that indicates which machine should be contacted by
        others to bootstrap and get initial service information.
      additional_creds: A dict that specifies arbitrary credentials that should
        also be passed in with the generated parameters.
    Returns:
      A dict whose keys indicate API service information as well as a special
      key that indicates machine to role mapping information.
    """
    creds = {
      "table" : options.table,
      "hostname" : first_host,
      "ips" : json.dumps(node_layout.to_dict_without_head_node()),
      "keyname" : options.keyname,
      "replication" : str(node_layout.replication_factor()),
      "appengine" : str(options.appengine),
      "autoscale" : str(options.autoscale),
      "group" : options.group
    }
    creds.update(additional_creds)

    if options.infrastructure:
      iaas_creds = {
        'machine' : options.machine,
        'instance_type' : options.instance_type,
        'infrastructure' : options.infrastructure,
        'min_images' : node_layout.min_vms,
        'max_images' : node_layout.max_vms
      }
      creds.update(iaas_creds)

    return creds


  @classmethod
  def obscure_dict(cls, dict_to_obscure):
    """Creates a copy of the given dictionary, but replaces values that may be
    too sensitive to print to standard out or log with a partially masked
    version.

    Args:
      dict_to_obscure: The dictionary whose values we wish to obscure.
    Returns:
      A dictionary with the same keys as dict_to_obscure, but with values that
      are masked if the key relates to a cloud credential.
    """
    obscured = {}
    obscure_regex = re.compile('[EC2]|[ec2]')
    for key, value in dict_to_obscure.iteritems():
      if obscure_regex.match(key):
        obscured[key] = cls.obscure_str(value)
      else:
        obscured[key] = value

    return obscured


  @classmethod
  def obscure_str(cls, str_to_obscure):
    """Obscures the given string by replacing all but four of its characters
    with asterisks.

    Args:
      str_to_obscure: The str that we wish to obscure.
    Returns:
      A str whose contents have been replaced by asterisks, except for the
      trailing 4 characters.
    """
    if len(str_to_obscure) < 4:
      return str_to_obscure
    last_four = str_to_obscure[len(str_to_obscure)-4:len(str_to_obscure)]
    return "*" * (len(str_to_obscure) - 4) + last_four


  @classmethod
  def generate_ssl_cert(cls, keyname):
    """Generates a self-signed SSL certificate that AppScale services can use
    to encrypt traffic with.

    Args:
      keyname: A str representing the SSH keypair name used for this AppScale
        deployment.
    """
    # lifted from http://sheogora.blogspot.com/2012/03/m2crypto-for-python-x509-certificates.html
    key = M2Crypto.RSA.gen_key(2048, 65537)

    pkey = M2Crypto.EVP.PKey()
    pkey.assign_rsa(key)

    cur_time = M2Crypto.ASN1.ASN1_UTCTIME()
    cur_time.set_time(int(time.time()) - 60*60*24)
    expire_time = M2Crypto.ASN1.ASN1_UTCTIME()

    # Expire certs in 10 years.
    expire_time.set_time(int(time.time()) + 60 * 60 * 24 * 365 * 10)

    # creating a certificate
    cert = M2Crypto.X509.X509()
    cert.set_pubkey(pkey)
    cs_name = M2Crypto.X509.X509_Name()
    cs_name.C = "US"
    cs_name.CN = "appscale.com"
    cs_name.Email = "support@appscale.com"
    cert.set_subject(cs_name)
    cert.set_issuer_name(cs_name)
    cert.set_not_before(cur_time)
    cert.set_not_after(expire_time)

    # self signing a certificate
    cert.sign(pkey, md="sha256")

    key.save_key(LocalState.get_private_key_location(keyname), None)
    cert.save_pem(LocalState.get_certificate_location(keyname))


  @classmethod
  def get_key_path_from_name(cls, keyname):
    """Determines the location where the SSH private key used to log into the
    virtual machines in this AppScale deployment can be found.

    Args:
      keyname: A str that indicates the name of the SSH keypair that
        uniquely identifies this AppScale deployment.
    Returns:
      A str that indicates where the private key can be found.
    """
    return cls.LOCAL_APPSCALE_PATH + keyname + ".key"


  @classmethod
  def get_private_key_location(cls, keyname):
    """Determines the location where the private key used to sign the
    self-signed certificate used for this AppScale deployment can be found.

    Args:
      keyname: A str that indicates the name of the SSH keypair that
        uniquely identifies this AppScale deployment.
    Returns:
      A str that indicates where the private key can be found.
    """
    return cls.LOCAL_APPSCALE_PATH + keyname + "-key.pem"


  @classmethod
  def get_certificate_location(cls, keyname):
    """Determines the location where the self-signed certificate for this
    AppScale deployment can be found.

    Args:
      keyname: A str that indicates the name of the SSH keypair that
        uniquely identifies this AppScale deployment.
    Returns:
      A str that indicates where the self-signed certificate can be found.
    """
    return cls.LOCAL_APPSCALE_PATH + keyname + "-cert.pem"


  @classmethod
  def get_locations_yaml_location(cls, keyname):
    """Determines the location where the YAML file can be found that contains
    information not related to service placement (e.g., what cloud we're
    running on, security group names).

    Args:
      keyname: A str that indicates the name of the SSH keypair that
        uniquely identifies this AppScale deployment.
    Returns:
      A str that indicates where the locations.yaml file can be found.
    """
    return cls.LOCAL_APPSCALE_PATH + "locations-" + keyname + ".yaml"


  @classmethod
  def get_locations_json_location(cls, keyname):
    """Determines the location where the JSON file can be found that contains
    information related to service placement (e.g., where machines can be found
    and what services they run).

    Args:
      keyname: A str that indicates the name of the SSH keypair that
        uniquely identifies this AppScale deployment.
    Returns:
      A str that indicates where the locations.json file can be found.
    """
    return cls.LOCAL_APPSCALE_PATH + "locations-" + keyname + ".json"


  @classmethod
  def update_local_metadata(cls, options, node_layout, host, instance_id):
    """Writes a locations.yaml and locations.json file to the local filesystem,
    that the tools can use to locate machines in an AppScale deployment.

    Args:
      options: A Namespace that indicates deployment-specific parameters not
        relating to the placement strategy in use.
      node_layout: A NodeLayout that indicates the placement strategy in use
        for this deployment.
      host: A str representing the location we can reach an AppController at.
      instance_id: The instance ID (if running in a cloud environment)
        associated with the given host.
    """
    # find out every machine's IP address and what they're doing
    acc = AppControllerClient(host, cls.get_secret_key(options.keyname))
    all_ips = [str(ip) for ip in acc.get_all_public_ips()]
    role_info = acc.get_role_info()

    infrastructure = options.infrastructure or 'xen'

    # write our yaml metadata file
    yaml_contents = {
      'load_balancer' : str(host),
      'instance_id' : str(instance_id),
      'table' : options.table,
      'secret' : cls.get_secret_key(options.keyname),
      'db_master' : node_layout.db_master().id,
      'ips' : all_ips,
      'infrastructure' : infrastructure,
      'group' : options.group
    }
    with open(cls.get_locations_yaml_location(options.keyname), 'w') as file_handle:
      file_handle.write(yaml.dump(yaml_contents, default_flow_style=False))

    # and now we can write the json metadata file
    with open(cls.get_locations_json_location(options.keyname), 'w') as file_handle:
      file_handle.write(json.dumps(role_info))

  
  @classmethod
  def get_local_nodes_info(cls, keyname):
    """Reads the JSON-encoded metadata on disk and returns a list that indicates
    which machines run each API service in this AppScale deployment.

    Args:
      keyname: A str that represents an SSH keypair name, uniquely identifying
        this AppScale deployment.
    Returns:
      A list of dicts, where each dict contains information on a single machine
      in this AppScale deployment.
    """
    with open(cls.get_locations_json_location(keyname), 'r') as file_handle:
      return json.loads(file_handle.read())


  @classmethod
  def get_host_for_role(cls, keyname, role):
    for node in cls.get_local_nodes_info(keyname):
      if role in node["jobs"]:
          return node["public_ip"]


  @classmethod
  def encrypt_password(cls, username, password):
    """Salts the given password with the provided username and encrypts it.

    Args:
      username: A str representing the username whose password we wish to
        encrypt.
      password: A str representing the password to encrypt.
    Returns:
      The SHA1-encrypted password.
    """
    return hashlib.sha1(username + password).hexdigest()


  @classmethod
  def get_login_host(cls, keyname):
    """Searches through the local metadata to see which virtual machine runs the
    login service.

    Args:
      keyname: The SSH keypair name that uniquely identifies this AppScale
        deployment.
    Returns:
      A str containing the host that runs the login service.
    """
    return cls.get_host_with_role(keyname, 'login')


  @classmethod
  def get_host_with_role(cls, keyname, role):
    """Searches through the local metadata to see which virtual machine runs the
    specified role.

    Args:
      keyname: The SSH keypair name that uniquely identifies this AppScale
        deployment.
      role: A str indicating the role to search for.
    Returns:
      A str containing the host that runs the specified service.
    """
    nodes = cls.get_local_nodes_info(keyname)
    for node in nodes:
      if role in node['jobs']:
        return node['public_ip']
    raise AppScaleException("Couldn't find a {0} node.".format(role))


  @classmethod
  def get_credentials(cls, is_admin=True):
    """Queries the user for the username and password that should be set for the
    cloud administrator's account in this AppScale deployment.

    Args:
      is_admin: A bool that indicates if we should be prompting the user for an
        admin username/password or not.

    Returns:
      A tuple containing the username and password that the user typed in.
    """
    username, password = None, None

    username = cls.get_username_from_stdin(is_admin)
    password = cls.get_password_from_stdin()
    return username, password

  
  @classmethod
  def get_username_from_stdin(cls, is_admin):
    """Asks the user for the name of the e-mail address that should be made an
    administrator on their AppScale cloud or App Engine application.

    Returns:
      A str containing the e-mail address the user typed in.
    """
    while True:
      if is_admin:
        username = raw_input('Enter your desired admin e-mail address: ')
      else:
        username = raw_input('Enter your desired e-mail address: ')

      email_regex = '^.+\\@(\\[?)[a-zA-Z0-9\\-\\.]+\\.([a-zA-Z]{2,3}|[0-9]{1,3})(\\]?)$'
      if re.match(email_regex, username):
        return username
      else:
        AppScaleLogger.warn('Invalid e-mail address. Please try again.')


  @classmethod
  def get_password_from_stdin(cls):
    """Asks the user for the password that should be used for their user
    account.

    Args:
      username: A str representing the email address associated with the user's
        account.
    Returns:
      The SHA1-hashed version of the password the user typed in.
    """
    while True:
      password = getpass.getpass('Enter new password: ')
      if len(password) < 6:
        AppScaleLogger.warn('Password must be at least 6 characters long')
        continue
      password_confirmation = getpass.getpass('Confirm password: ')
      if password == password_confirmation:
        return password
      else:
        AppScaleLogger.warn('Passwords entered do not match. Please try again.')


  @classmethod
  def map_to_array(cls, the_map):
    """Converts a dict into list. Given a map {k1:v1, k2:v2,...kn:vn}, this will
    return a list [k1,v1,k2,v2,...,kn,vn].

    Args:
      the_map: A dictionary of objects to convert into a list.

    Returns:
      A list containing all the keys and values in the input dictionary.
    """
    the_list = []
    for key, value in the_map.items():
      the_list.append(key)
      the_list.append(value)
    return the_list


  @classmethod
  def get_infrastructure(cls, keyname):
    """Reads the locations.yaml file to see if this AppScale deployment is
    running over a cloud infrastructure or a virtualized cluster.

    Args:
      keyname: The SSH keypair name that uniquely identifies this AppScale
        deployment.
    Returns:
      The name of the cloud infrastructure that AppScale is running over, or
      'xen' if running over a virtualized cluster.
    """
    with open(cls.get_locations_yaml_location(keyname), 'r') as file_handle:
      return yaml.safe_load(file_handle.read())["infrastructure"]


  @classmethod
  def get_group(cls, keyname):
    """Reads the locations.yaml file to see what security group was created for
    this AppScale deployment.

    Args:
      keyname: The SSH keypair name that uniquely identifies this AppScale
        deployment.
    Returns:
      The name of the security group used for this AppScale deployment.
    """
    with open(cls.get_locations_yaml_location(keyname), 'r') as file_handle:
      return yaml.safe_load(file_handle.read())["group"]


  @classmethod
  def cleanup_appscale_files(cls, keyname):
    """Removes all AppScale metadata files from this machine.

    Args:
      keyname: The SSH keypair name that uniquely identifies this AppScale
        deployment.
    """
    os.remove(LocalState.get_locations_yaml_location(keyname))
    os.remove(LocalState.get_locations_json_location(keyname))
    os.remove(LocalState.get_secret_key_location(keyname))


  def shell(cls, command, is_verbose, num_retries=DEFAULT_NUM_RETRIES):
    """Executes a command on this machine, retrying it if it initially fails.

    Args:
      command: A str representing the command to execute.
      is_verbose: A bool that indicates if we should print the command we are
        executing to stdout.
      num_retries: The number of times we should try to execute the given
        command before aborting.
    Returns:
      The standard output and standard error produced when the command executes.
    Raises:
      ShellException: If, after five attempts, executing the named command
      failed.
    """
    tries_left = num_retries
    while tries_left:
      AppScaleLogger.verbose("shell> {0}".format(command), is_verbose)
      the_temp_file = tempfile.TemporaryFile()
      result = subprocess.Popen(command, shell=True, stdout=the_temp_file,
        stderr=sys.stdout)
      result.wait()
      if result.returncode == 0:
        output = the_temp_file.read()
        the_temp_file.close()
        return output
      AppScaleLogger.verbose("Command failed. Trying again momentarily." \
        .format(command), is_verbose)
      tries_left -= 1
      time.sleep(1)
    raise ShellException('Could not execute command: {0}'.format(command))