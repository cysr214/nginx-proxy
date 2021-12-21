import re
import subprocess

import backoff
import docker
import pytest

docker_client = docker.from_env()


###############################################################################
#
# Tests helpers
#
###############################################################################

@backoff.on_exception(backoff.constant, AssertionError, interval=2, max_tries=15, jitter=None)
def assert_log_contains(expected_log_line, container_name="nginxproxy"):
    """
    Check that the nginx-proxy container log contains a given string.
    The backoff decorator will retry the check 15 times with a 2 seconds delay.

    :param expected_log_line: string to search for
    :return: None
    :raises: AssertError if the expected string is not found in the log
    """
    sut_container = docker_client.containers.get(container_name)
    docker_logs = sut_container.logs(stdout=True, stderr=True, stream=False, follow=False)
    assert bytes(expected_log_line, encoding="utf8") in docker_logs


def require_openssl(required_version):
    """
    This function checks that the required version of OpenSSL is present, and skips the test if not.
    Use it as a test function decorator:

        @require_openssl("2.3.4")
        def test_something():
            ...

    :param required_version: minimal required version as a string: "1.2.3"
    """

    def versiontuple(v):
        clean_v = re.sub(r"[^\d\.]", "", v)
        return tuple(map(int, (clean_v.split("."))))

    try:
        command_output = subprocess.check_output(["openssl", "version"])
    except OSError:
        return pytest.mark.skip("openssl command is not available in test environment")
    else:
        if not command_output:
            raise Exception("Could not get openssl version")
        openssl_version = str(command_output.split()[1])
        return pytest.mark.skipif(
            versiontuple(openssl_version) < versiontuple(required_version),
            reason=f"openssl v{openssl_version} is less than required version {required_version}")


@require_openssl("1.0.2")
def negotiate_cipher(sut_container, additional_params='', grep='Cipher is'):
    host = f"{sut_container.attrs['NetworkSettings']['IPAddress']}:443"

    try:
        # Enforce TLS 1.2 as newer versions don't support custom dhparam or ciphersuite preference.
        # The empty `echo` is to provide `openssl` user input, so that the process exits: https://stackoverflow.com/a/28567565
        # `shell=True` enables using a single string to execute as a shell command.
        # `text=True` prevents the need to compare against byte strings.
        # `stderr=subprocess.PIPE` removes the output to stderr being interleaved with test case status (output during exceptions).
        return subprocess.check_output(
            f"echo '' | openssl s_client -connect {host} -tls1_2 {additional_params} | grep '{grep}'",
            shell=True,
            text=True,
            stderr=subprocess.PIPE,
        )
    except subprocess.CalledProcessError as e:
        # Output a more helpful error, the original exception in this case isn't that helpful.
        # `from None` to ignore undesired output from exception chaining.
        raise Exception("Failed to process CLI request:\n" + e.stderr) from None


# The default `dh_bits` can vary due to configuration.
# `additional_params` allows for adjusting the request to a specific `VIRTUAL_HOST`,
# where DH size can differ from the configured global default DH size.
def can_negotiate_dhe_ciphersuite(sut_container, dh_bits=4096, additional_params=''):
    openssl_params = f"-cipher 'EDH' {additional_params}"

    r = negotiate_cipher(sut_container, openssl_params)
    assert "New, TLSv1.2, Cipher is DHE-RSA-AES256-GCM-SHA384\n" == r

    r2 = negotiate_cipher(sut_container, openssl_params, "Server Temp Key")
    assert f"Server Temp Key: DH, {dh_bits} bits" in r2


def cannot_negotiate_dhe_ciphersuite(sut_container):
    # Fail to negotiate a DHE cipher suite:
    r = negotiate_cipher(sut_container, "-cipher 'EDH'")
    assert "New, (NONE), Cipher is (NONE)\n" == r

    # Correctly establish a connection (TLS 1.2):
    r2 = negotiate_cipher(sut_container)
    assert "New, TLSv1.2, Cipher is ECDHE-RSA-AES256-GCM-SHA384\n" == r2

    r3 = negotiate_cipher(sut_container, grep="Server Temp Key")
    assert "X25519" in r3


def should_be_equivalent_content(sut_container, expected, actual):
    expected_checksum = sut_container.exec_run(f"md5sum {expected}").output.split()[0]
    actual_checksum = sut_container.exec_run(f"md5sum {actual}").output.split()[0]

    assert expected_checksum == actual_checksum


# Parse array of container ENV, splitting at the `=` and returning the value, otherwise `None`
def get_env(sut_container, var):
  env = sut_container.attrs['Config']['Env']

  for e in env:
    if e.startswith(var):
      return e.split('=')[1]
  
  return None


###############################################################################
#
# Tests
#
###############################################################################

def test_default_dhparam_is_ffdhe4096(docker_compose):
    container_name="dh-default"
    sut_container = docker_client.containers.get(container_name)
    assert sut_container.status == "running"

    assert_log_contains("Setting up DH Parameters..", container_name)

    # `dhparam.pem` contents should match the default (ffdhe4096.pem):
    should_be_equivalent_content(
        sut_container,
        "/app/dhparam/ffdhe4096.pem",
        "/etc/nginx/dhparam/dhparam.pem"
    )

    can_negotiate_dhe_ciphersuite(sut_container, 4096)


# Overrides default DH group via ENV `DHPARAM_BITS=3072`:
def test_can_change_dhparam_group(docker_compose):
    container_name="dh-env"
    sut_container = docker_client.containers.get(container_name)
    assert sut_container.status == "running"

    assert_log_contains("Setting up DH Parameters..", container_name)

    # `dhparam.pem` contents should not match the default (ffdhe4096.pem):
    should_be_equivalent_content(
        sut_container,
        "/app/dhparam/ffdhe3072.pem",
        "/etc/nginx/dhparam/dhparam.pem"
    )

    can_negotiate_dhe_ciphersuite(sut_container, 3072)


def test_fail_if_dhparam_group_not_supported(docker_compose):
    container_name="invalid-group-1024"
    sut_container = docker_client.containers.get(container_name)
    assert sut_container.status == "exited"

    DHPARAM_BITS = get_env(sut_container, "DHPARAM_BITS")
    assert DHPARAM_BITS == "1024"

    assert_log_contains(
        f"ERROR: Unsupported DHPARAM_BITS size: {DHPARAM_BITS}. Use: 2048, 3072, or 4096 (default).",
        container_name
    )


# Overrides default DH group by providing a custom `/etc/nginx/dhparam/dhparam.pem`:
def test_custom_dhparam_is_supported(docker_compose):
    container_name="dh-file"
    sut_container = docker_client.containers.get(container_name)
    assert sut_container.status == "running"

    assert_log_contains(
        "Warning: A custom dhparam.pem file was provided. Best practice is to use standardized RFC7919 DHE groups instead.",
        container_name
    )

    # `dhparam.pem` contents should not match the default (ffdhe4096.pem):
    should_be_equivalent_content(
        sut_container,
        "/app/dhparam/ffdhe3072.pem",
        "/etc/nginx/dhparam/dhparam.pem"
    )

    can_negotiate_dhe_ciphersuite(sut_container, 3072)


def test_can_skip_dhparam(docker_compose):
    container_name="dh-skip"
    sut_container = docker_client.containers.get(container_name)
    assert sut_container.status == "running"

    assert_log_contains("Skipping Diffie-Hellman parameters setup.", container_name)

    cannot_negotiate_dhe_ciphersuite(sut_container)


def test_can_skip_dhparam_backward_compatibility(docker_compose):
    container_name="dh-skip-backward"
    sut_container = docker_client.containers.get(container_name)
    assert sut_container.status == "running"
    
    assert_log_contains("Warning: The DHPARAM_GENERATION environment variable is deprecated, please consider using DHPARAM_SKIP set to true instead.", container_name)
    assert_log_contains("Skipping Diffie-Hellman parameters setup.", container_name)

    cannot_negotiate_dhe_ciphersuite(sut_container)


def test_web5_https_works(docker_compose, nginxproxy):
    r = nginxproxy.get("https://web5.nginx-proxy.tld/port", allow_redirects=False)
    assert r.status_code == 200
    assert "answer from port 85\n" in r.text
