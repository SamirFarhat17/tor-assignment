import logging
import os
import sys
import time
from argparse import ArgumentParser
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

from dependency.torpy import TorClient
from dependency.torpy.cells import (
    CellRelayExtend2, CellRelayExtended2, CellRelayEarly, CellRelayTruncated, CellCreated2, CellCreate2, CellCreateFast)
from dependency.torpy.crypto import TOR_DIGEST_LEN, kdf_tor
from dependency.torpy.crypto_common import dh_shared, dh_public_from_bytes, hkdf_sha256, curve25519_get_shared
from dependency.torpy.guard import TorGuard
from dependency.torpy.keyagreement import FastKeyAgreement, KeyAgreement, KeyAgreementError
from dependency.torpy.utils import register_logger, recv_all
from dependency.torpy.circuit import random, TorCircuit, CircuitNode, NtorKeyAgreement
from dependency.torpy.documents.network_status import RouterFlags

# Helper functions
# Generate a unique 4-byte id for a circuit
CIRCUIT_ID = random.randrange(0, 0xFFFFFFFF)


def gen_circuit_id():
    global CIRCUIT_ID
    CIRCUIT_ID = ((CIRCUIT_ID + 1) & 0xFFFFFFFF) | 0x80000000
    return CIRCUIT_ID


# Get a list of all guard nodes and middle nodes in the consensus
def get_all_relays(consensus):
    return consensus.get_routers([RouterFlags.Authority], has_dir_port=True, with_renew=True)


# Get a list of all exit nodes in the consensus
def get_all_exits(consensus):
    return consensus.get_routers([RouterFlags.Exit], exclude_flags=[RouterFlags.Authority], has_dir_port=True,
                                 with_renew=True)


def random_router(onion_routers):
    router = random.choice(onion_routers)
    logger.info('Selected node %s:%d AKA %s' % (router.ip, router.dir_port, router.nickname))
    return router


# Lookup router by its IP address and return its descriptor as a router object
def router_from_ip(ip, consensus):
    all_routers = consensus.get_routers([], has_dir_port=True, with_renew=True)
    matching_routers = list(filter(lambda r: ip == ('%s:%d' % (r.ip, r.dir_port)), all_routers))
    if matching_routers:
        router = matching_routers.pop()
        logger.info('Selected node %s:%d AKA %s' % (router.ip, router.dir_port, router.nickname))
        return router
    raise LookupError("No routers with the IP address %s" % ip)


# Create a TCP layer within Tor pointed to some external end-point
# This stream can .send(b'...') a cell as bytes, and .recv(1024) a cell back.
def new_tcp_stream(circuit, hostname, port):
    return circuit.create_stream((hostname, port))


# Build CREATE cell with digest and circuit ID
def build_create_cell(digest, circuit_id):
    return CellCreateFast(digest, circuit_id)


# Build EXTEND cell to pass on to a new router and extend the routing path
def build_extend_cell(router, onion_skin):
    return CellRelayExtend2(router.ip, router.or_port, router.fingerprint, onion_skin)


# Send a Tor CREATE cell and await an CREATED cell as the response
def send_receive_cell_create(cell_create, circuit, circuit_node):
    acknowledgement = CellCreateFast  # Wait for this type of response
    return circuit.send_wait(cell_create, circuit_node, acknowledgement)


# Send a Tor EXTEND cell and await an EXTENDED cell as the response
def send_receive_cell_extend(cell_extend, circuit):
    acknowledgement = [CellRelayExtended2, CellRelayTruncated]  # Wait for this type of response
    return circuit.send_relay_wait(cell_extend, acknowledgement, relay_type=CellRelayEarly)


# Computes g^x, for example, or X^y, etc.
def raise_exponent(base, exponent):
    return curve25519_get_shared(
        X25519PrivateKey.from_private_bytes(exponent),
        X25519PublicKey.from_public_bytes(base)
    )


def random_bytes(count):
    return os.urandom(count)


#
# Your implementation
#

# Adds a relay hop to a circuit object
def extend(circuit, node_router):
    """
    Section 5.3 - Send CellExtend to extend circuit (Circuit).

    To extend the circuit by a single onion router R_M, the OP performs these steps:
        1. Create an onion skin, encrypted to R_M's public onion key.
        2. Send the onion skin in a relay EXTEND2 cell along
           the circuit (see sections 5.1.2 and 5.5).
        3. When a relay EXTENDED/EXTENDED2 cell is received, verify KH,
           and calculate the shared keys.  The circuit is now extended.
    """
    logger.info('Extending the circuit #%x with %s...', circuit.id, node_router)
    # not sure if need to use router from ip here or not
    # router_from_ip(node_router, consensus)
    node_extended = CircuitNode(node_router, NtorKeyAgreement)

    key_agreement = node_extended.key_agreement  # See section 5.1.4 for full specification

    node_ID = key_agreement._fingerprint_bytes  # Server node's fingerprint
    private_x = key_agreement._x._raw_private_bytes()  # Client's private key, x
    public_X = key_agreement._X._raw_public_bytes()  # Client's public key, X
    public_B = key_agreement._B._raw_public_bytes()  # Node's public "ntor onion" key, B

    # concatenation ID | B | X
    # not sure if it should be private or public x
    onion_skin = b"".join([node_ID, public_B, public_X])  # your-code-here#

    # Build an EXTEND cell with the new node's info and our known keys
    extend_cell = build_extend_cell(node_router, onion_skin)  # your-code-here#

    # Send EXTEND cell to the next node and receive an EXTENDED cell back
    extended_cell = send_receive_cell_extend(extend_cell, circuit)  # your-code-here#

    # Meanwhile, the server at the new node generates a keypair of y,Y = KEYGEN(), and uses its ntor
    # onion private key, b, to compute H(H(X^y|X^b|ID|B|X|Y, t_verify)|ID|B|Y|X)
    #
    # All you need to know is that X = g^x and Y = g^y.
    #
    # Given Y from the client, compute X^y and X^b, and concatenate them to get a shared secret input.
    # This will serve as input to HKDF-SHA256 for derivation of this layer's encryption keys.

    public_Y = extended_cell.handshake_data[:32]  # Node's public key, Y
    auth_digest = extended_cell.handshake_data[32:]
    shared_X__y = raise_exponent(public_Y, private_x)  # your-code-here#
    shared_X__b = raise_exponent(public_B, private_x)  # your-code-here#
    # not sure if protoid is right there (look at 5.1.4)
    secret_input = b"".join([shared_X__y, shared_X__b])  # your-code-here#

    # Complete the remaining hashing, verification - for further reference, read section 5.1.4 and 5.2.2.
    shared_secret = node_extended.complete_handshake(secret_input, public_Y, auth_digest)

    node_extended.store_key(shared_secret)

    circuit.circuit_nodes.append(node_extended)


# Takes a circuit object containing only a guard, and extends it to contain both a middle and an exit
def circuit_build_hops(circuit, middle_router, exit_router):
    logger.info('Building 3 hops circuit...')
    # your-code-here#
    extend(circuit, middle_router)
    extend(circuit, exit_router)

    logger.debug('Circuit has been built')

    return circuit


# Create a circuit by establishing a shared secret with a first node
def circuit_from_guard(guard_router, circuit_id):
    logger.debug('[TorInfo] Create new base circuit from %s', guard_router.nickname)
    guard = TorGuard(guard_router, purpose='TorClient')
    circuit = TorCircuit(circuit_id, guard)
    circuit_node = CircuitNode(guard_router, key_agreement_cls=FastKeyAgreement)

    # We need to build a CREATE cell containing random digest (X) and the new circuit ID.
    # Then, we need to send it to the first node in the circuit we are trying to create here.
    # Note that TOR_DIGEST_LEN = HASH_LEN = 20 bytes

    # create random digest of len 20 bytes
    x = random_bytes(20)  # your-code-here#
    cell_create = build_create_cell(x, circuit_id)  # your-code-here#
    cell_created = send_receive_cell_create(cell_create, circuit, circuit_node)  # your-code-here#

    # Extract the two parts from the received CREATED cell
    y = cell_created.handshake_data[:TOR_DIGEST_LEN]  # Key material (Y)
    key_hash = cell_created.handshake_data[TOR_DIGEST_LEN:]  # Derivative key data
    # Please reference 5.1.5 and 5.2.1 of the Tor protocol specification for how to compute K_0 before hashing.
    k0 = b"".join([x, y])  # your-code-here#
    k = kdf_tor(k0)

    computed_auth, shared_secret = k

    # Compare values to verify that the node received X correctly.
    if computed_auth != key_hash:
        raise KeyAgreementError('Auth input does not match.')
    circuit_node.store_key(shared_secret)

    return circuit


# Initiates the construction of a circuit and then gets a web page through that circuit
def get(hostname, port, path="", guard_address=None, middle_address=None, exit_address=None):
    live = False
    # tor = TorClient(use_local_directories=(live))  # task 4
    tor = TorClient()  # task 2
    # - Your code here - Pick nodes to form your circuit, an ID,
    # and establish a layered connection between them. This is
    # the Tor circuit that will be used to request a web page.
    consensus = tor.consensus

    all_relays = get_all_relays(consensus)
    all_exits = get_all_exits(consensus)
    if guard_address is None:
        router = random_router(all_relays)
        guard_address = str(router.ip) + ":" + str(router.dir_port)
        router = random_router(all_relays)
        middle_address = str(router.ip) + ":" + str(router.dir_port)
        while middle_address == guard_address:
            router = random_router(all_relays)
            middle_address = str(router.ip) + ":" + str(router.dir_port)
        router = random_router(all_exits)
        exit_address = str(router.ip) + ":" + str(router.dir_port)
        while middle_address == exit_address or guard_address == exit_address:
            router = random_router(all_exits)
            exit_address = str(router.ip) + ":" + str(router.dir_port)

    # your-code-here#
    circ_id = gen_circuit_id()
    circuit_base = circuit_from_guard(router_from_ip(guard_address, consensus), circ_id)  # your-code-here#  # CREATE
    circuit = circuit_build_hops(
        circuit_base,
        router_from_ip(middle_address, consensus),
        router_from_ip(exit_address, consensus)
    )  # your-code-here#  # EXTEND

    # Use our established circuit to attach a TCP stream
    port = port or 80

    stream = new_tcp_stream(circuit_base, hostname, port)  # your-code-here#  # BEGIN
    # Make an HTTP GET request to the web page at <hostname>:<port>/<path>
    # request = hostname + ":" + str(port) + "/" + path
    request = b'GET /%s HTTP/1.0\r\nHost: %s\r\n\r\n' % (path.encode(), hostname.encode())  # your-code-here#
    logger.warning('Sending: %s %s:%s', request, hostname, port)
    # your-code-here#
    stream.send(request)
    logger.debug('Reading...')
    recv = recv_all(stream)  # your-code-here#
    circuit_base._guard.close()
    stream.close()
    tor.close()
    return recv.decode('utf-8')


def main():
    parser = ArgumentParser()
    parser.add_argument('--url', help='url', required=True)
    parser.add_argument('--mode', default='random', type=str.lower, help='random or specific route')
    parser.add_argument('--guard', default=None, type=str, help='guard node ip address')
    parser.add_argument('--middle', default=None, type=str, help='middle node ip address')
    parser.add_argument('--exit', default=None, type=str, help='exit node ip address')
    parser.add_argument('--outfile', default="", type=str, help='output file path')
    args = parser.parse_args()
    url = urlparse(args.url)
    start = time.time()
    response = get(
        hostname=url.hostname,
        port=url.port, path=url.path,
        guard_address=args.guard,
        middle_address=args.middle,
        exit_address=args.exit
    )
    end = time.time()

    # Write response to args.outfile or stdout
    if args.outfile == "":
        print('response', response)
    else:
        outfile = open(args.outfile, "w")
        outfile.write(response)
        outfile.close()
        # print(f"Time for run is {end - start}")



if __name__ == '__main__':
    logger = logging.getLogger("")
    register_logger(verbose=0)
    try:
        main()
    except KeyboardInterrupt:
        logger.error('Interrupted.')
