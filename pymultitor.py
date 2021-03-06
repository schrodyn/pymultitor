import re
import sys
import json
import atexit
import socket
import logging
import requests
import platform
import itertools
from os import path
from time import sleep
from shutil import rmtree
from mitmproxy import ctx
from tempfile import mkdtemp
from mitmproxy.http import HTTPResponse
from mitmproxy.tools.main import mitmdump
from multiprocessing.pool import ThreadPool
from stem.control import Controller, Signal
from requests.exceptions import ConnectionError
from stem.process import launch_tor_with_config
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter

__version__ = '3.2.0'


def is_windows():
    return platform.system().lower() == 'windows'


def monkey_patch():
    _log_mitmproxy = logging.getLogger("mitmproxy")

    # Patch mitmproxy.log.Log.__call__
    from mitmproxy import log

    def _log__call__(self, text, level="info"):
        getattr(_log_mitmproxy, level)(text)

    setattr(log.Log, "__call__", _log__call__)

    # Patch mitmproxy.addons.termlog.log
    from mitmproxy.addons import termlog

    def _termlog_log(self, e):
        getattr(_log_mitmproxy, e.level)(e.msg)

    setattr(termlog.TermLog, "log", _termlog_log)

    # Patch mitmproxy.addon.dumper.echo & mitmproxy.addon.dumper.echo_error
    from mitmproxy.addons import dumper

    def _dumper_echo(self, text, ident=None, **style):
        if ident:
            text = dumper.indent(ident, text)
        _log_mitmproxy.info(text)

    setattr(dumper.Dumper, "echo", _dumper_echo)

    def _dumper_echo_error(self, text, **style):
        _log_mitmproxy.error(text)

    setattr(dumper.Dumper, "echo_error", _dumper_echo_error)


class Tor(object):
    def __init__(self, cmd='tor', config="{}"):
        self.logger = logging.getLogger(__name__)
        self.tor_cmd = cmd
        self.tor_config = config or {}
        self.socks_port = self.free_port()
        self.control_port = self.free_port()
        self.data_directory = mkdtemp()
        self.id = self.socks_port
        self.process = None
        self.controller = None
        self.__is_shutdown = False

    def __del__(self):
        self.shutdown()

    def __enter__(self):
        return self.run()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.shutdown()

    def run(self):
        self.logger.debug(f"[{self.id:05d}] Executing Tor Process")
        self.process = launch_tor_with_config(
            config={
                "ControlPort": str(self.control_port),
                "SOCKSPort": str(self.socks_port),
                "DataDirectory": self.data_directory,
                "AllowSingleHopCircuits": "1",
                "ExcludeSingleHopRelays": "0",
                **self.tor_config
            },
            tor_cmd=self.tor_cmd,
            init_msg_handler=self.print_bootstrapped_line
        )

        self.logger.debug(f"[{self.id:05d}] Creating Tor Controller")
        self.controller = Controller.from_port(port=self.control_port)
        self.controller.authenticate()

        return self

    def shutdown(self):
        if self.__is_shutdown:
            return

        self.__is_shutdown = True
        self.logger.debug(f"[{self.id:05d}] Destroying Tor")
        self.controller.close()
        self.process.terminate()
        self.process.wait()

        # If Not Closed Properly
        if path.exists(self.data_directory):
            rmtree(self.data_directory)

    def newnym_available(self):
        return self.controller.is_newnym_available()

    def newnym_wait(self):
        return self.controller.get_newnym_wait()

    def newnym(self):
        if not self.newnym_available():
            self.logger.debug(f"[{self.id:05d}] Could Not Change Tor Identity (Wait {round(self.newnym_wait())}s)")
            return False

        self.logger.info(f"[{self.id:05d}] Changing Tor Identity")
        self.controller.signal(Signal.NEWNYM)
        return True

    def print_bootstrapped_line(self, line):
        if "Bootstrapped" in line:
            self.logger.debug(f"[{self.id:05d}] Tor Bootstrapped Line: {line}")

            if "100%" in line:
                self.logger.debug(f"[{self.id:05d}] Tor Process Executed Successfully")

    @staticmethod
    def free_port():
        """
        Determines a free port using sockets.
        Taken from selenium python.
        """
        free_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        free_socket.bind(('0.0.0.0', 0))
        free_socket.listen(5)
        port = free_socket.getsockname()[1]
        free_socket.close()
        return port


class MultiTor(object):
    def __init__(self, size=2, cmd='tor', config=None):
        self.logger = logging.getLogger(__name__)
        self.cmd = cmd
        self.size = size
        self.list = []
        self.cycle = None
        self.current = None
        try:
            self.config = self.parse_config(config)
        except Exception as error:
            print(error, config, type(config))

    def parse_config(self, config=None):
        config = config or {}

        cfg = {}
        try:
            if isinstance(config, dict):
                cfg = config
            elif path.isfile(config):
                with open(config, encoding='utf-8') as cfg_file:
                    json.load(cfg_file)
            else:
                cfg = json.loads(config)
        except (TypeError, json.JSONDecodeError):
            self.logger.error(f"Could Not Parse Extended JSON Configuration {repr(config)}")
            return {}
        except Exception as error:
            self.logger.error(f"Got Unknown Error {error}")
            return {}

        # Remove Port / Data Configurations
        cfg.pop('ControlPort', None)
        cfg.pop('SOCKSPort', None)
        cfg.pop('DataDirectory', None)

        self.logger.debug(f"Extended Configuration: {json.dumps(cfg)}")
        return cfg

    def run(self):
        self.logger.info(f"Executing {self.size} Tor Processes")

        # If OS Platform Is Windows Run Processes Async
        if is_windows():
            pool = ThreadPool(processes=self.size)
            self.list = pool.map(lambda _: Tor(cmd=self.cmd, config=self.config).run(), range(self.size))
        else:
            self.list = [Tor(cmd=self.cmd).run() for _ in range(self.size)]

        self.logger.info("All Tor Processes Executed Successfully")
        self.cycle = itertools.cycle(self.list)
        self.current = next(self.cycle)

    @property
    def proxy(self):
        proxy_url = f'socks5://127.0.0.1:{self.current.socks_port:d}'
        return {'http': proxy_url, 'https': proxy_url}

    def new_identity(self):
        identity_changed = False
        while not identity_changed:
            identity_changed = self.current.newnym()
            self.current = next(self.cycle)
            if not identity_changed:
                sleep(0.1)

        return self.proxy

    def shutdown(self):
        for tor in self.list:
            tor.shutdown()


class PyMultiTor(object):
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.insecure = False

        # Change IP Policy (Configuration)
        self.counter = itertools.count()
        self.on_count = 0
        self.on_string = ""
        self.on_regex = ""
        self.on_rst = False
        self.on_error_code = 0

        self.multitor = None

    def load(self, loader):
        # MultiTor Configuration
        loader.add_option(
            name="tor_processes",
            typespec=int,
            default=2,
            help="number of tor processes in the cycle",
        )
        loader.add_option(
            name="tor_cmd",
            typespec=str,
            default='tor',
            help="tor cmd (executable path + arguments)",
        )
        loader.add_option(
            name="tor_config",
            typespec=str,
            default="{}",
            help="tor extended json configuration",
        )

        # When To Change IP Address
        loader.add_option(
            name="on_count",
            typespec=int,
            default=0,
            help="change ip every x requests (resources also counted)",
        )
        loader.add_option(
            name="on_string",
            typespec=str,
            default="",
            help="change ip when string found in the response content",
        )
        loader.add_option(
            name="on_regex",
            typespec=str,
            default="",
            help="change ip when regex found in The response content",
        )
        loader.add_option(
            name="on_rst",
            typespec=bool,
            default=False,
            help="change ip when connection closed with tcp rst",
        )
        loader.add_option(
            name="on_error_code",
            typespec=int,
            default=0,
            help="change ip when a specific status code returned",
        )

    def configure(self, updates):
        # Configure Logger
        logging.basicConfig(level=logging.DEBUG if ctx.options.termlog_verbosity.lower() == 'debug' else logging.INFO,
                            format='%(asctime)s %(levelname)-8s %(message)s',
                            datefmt='%d-%m-%y %H:%M:%S')

        # Disable Loggers
        monkey_patch()
        for logger_name in ["stem", "urllib3.connectionpool", "mitmproxy"]:
            logging.getLogger(logger_name).disabled = True

        # Log CMD Args If Debug Mode Enabled
        cmd_args = json.dumps({update: getattr(ctx.options, update) for update in updates})
        self.logger.debug(f"Running With CMD Args: {cmd_args}")

        self.on_count = ctx.options.on_count
        self.on_string = ctx.options.on_string
        self.on_regex = ctx.options.on_regex
        self.on_rst = ctx.options.on_rst
        self.on_error_code = ctx.options.on_error_code

        self.insecure = ctx.options.ssl_insecure

        self.multitor = MultiTor(
            size=ctx.options.tor_processes,
            cmd=ctx.options.tor_cmd,
            config=ctx.options.tor_config
        )
        try:
            self.multitor.run()
        except KeyboardInterrupt:
            self.multitor.shutdown()

        atexit.register(self.multitor.shutdown)

        # Warn If No Change IP Configuration:
        if not any([self.on_count, self.on_string, self.on_regex, self.on_rst, self.on_error_code]):
            self.logger.warning("Change IP Configuration Not Set (Acting As Regular Tor Proxy)")

    def create_response(self, request):
        response = requests.request(
            method=request.method,
            url=request.url,
            data=request.content,
            headers=request.headers,
            allow_redirects=False,
            verify=not self.insecure,
            proxies=self.multitor.proxy,
            stream=False
        )

        # Content-Length and Transfer-Encoding set. This is expressly forbidden by RFC 7230 sec 3.3.2.
        if response.headers.get("Transfer-Encoding") == "chunked":
            response.headers.pop("Transfer-Encoding")

        return HTTPResponse.make(
            status_code=response.status_code,
            content=response.content,
            headers=dict(response.headers),
        )

    def request(self, flow):
        error_message = None
        try:
            flow.response = self.create_response(flow.request)
        except ConnectionError:
            # If TCP Rst Configured
            if self.on_rst:
                self.logger.debug("Got TCP Rst, While TCP Rst Configured")
                self.multitor.new_identity()
                # Set Response
                try:
                    flow.response = self.create_response(flow.request)
                except Exception as error:
                    error_message = f"Got Error: {error}"
            else:
                error_message = "Got TCP Rst, While TCP Rst Not Configured"
        except Exception as error:
            error_message = f"Got Error: {error}"

        # When There Is No Response
        if error_message:
            self.logger.error(error_message)
            flow.response = HTTPResponse.make(
                status_code=500,
                content=error_message,
                headers={
                    "Server": f"pymultitor/{__version__}"
                }
            )
            return

            # If String Found In Response Content
        if self.on_string and self.on_string in flow.response.text:
            self.logger.debug("String Found In Response Content")
            self.multitor.new_identity()
            # Set Response
            flow.response = self.create_response(flow.request)

        # If Regex Found In Response Content
        if self.on_regex and re.search(self.on_regex, flow.response.text, re.IGNORECASE):
            self.logger.debug("Regex Found In Response Content")
            self.multitor.new_identity()
            # Set Response
            flow.response = self.create_response(flow.request)

        # If Counter Raised To The Configured Number
        if self.on_count and not next(self.counter) % self.on_count:
            self.logger.debug("Counter Raised To The Configured Number")
            self.multitor.new_identity()

        # If A Specific Status Code Returned
        if self.on_error_code and self.on_error_code == flow.response.status_code:
            self.logger.debug("Specific Status Code Returned")
            self.multitor.new_identity()
            # Set Response
            flow.response = self.create_response(flow.request)


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    parser = ArgumentParser(formatter_class=ArgumentDefaultsHelpFormatter)
    parser.add_argument("-v", "--version", action="version", version="%(prog)s {ver}".format(ver=__version__))

    # Proxy Configuration
    parser.add_argument("-lh", "--host",
                        help="proxy listen host.",
                        dest="listen_host",
                        default="127.0.0.1")
    parser.add_argument("-lp", "--port",
                        help="proxy listen port",
                        dest="listen_port",
                        type=int,
                        default=8080)
    parser.add_argument("-s", "--socks",
                        help="use as socks proxy (not http proxy)",
                        action='store_true')
    parser.add_argument("-a", "--auth",
                        help="set proxy authentication (format: 'username:pass')",
                        dest="auth",
                        default="")
    parser.add_argument("-i", "--insecure",
                        help="insecure ssl",
                        action='store_true')
    parser.add_argument("-d", "--debug",
                        help="Debug Log.",
                        action="store_true")

    # MultiTor Configuration
    parser.add_argument("-p", "--tor-processes",
                        help="number of tor processes in the cycle",
                        dest="processes",
                        type=int,
                        default=2)
    parser.add_argument("-c", "--tor-cmd",
                        help="tor cmd (executable path + arguments)",
                        dest="cmd",
                        default="tor")
    parser.add_argument("-e", "--tor-config",
                        help="tor extended json configuration",
                        dest="config",
                        default="{}")

    # When To Change IP Address
    parser.add_argument("--on-count",
                        help="change ip every x requests (resources also counted)",
                        type=int,
                        default=0)
    parser.add_argument("--on-string",
                        help="change ip when string found in the response content",
                        default="")
    parser.add_argument("--on-regex",
                        help="change ip when regex found in The response content",
                        default="")
    parser.add_argument("--on-rst",
                        help="change ip when connection closed with tcp rst",
                        action="store_true")
    parser.add_argument("--on-error-code",
                        help="change ip when a specific status code returned",
                        type=int,
                        default=0)

    sys_args = vars(parser.parse_args(args=args))
    mitmdump_args = [
        '--scripts', __file__,
        '--mode', 'socks5' if sys_args['socks'] else 'regular',
        '--listen-host', sys_args['listen_host'],
        '--listen-port', str(sys_args['listen_port']),
        '--set', f'tor_cmd={sys_args["cmd"]}',
        '--set', f'tor_config={sys_args["config"]}',
        '--set', f'tor_processes={sys_args["processes"]}',
        '--set', f'on_string={sys_args["on_string"]}',
        '--set', f'on_regex={sys_args["on_regex"]}',
        '--set', f'on_count={sys_args["on_count"]}',
        '--set', f'on_error_code={sys_args["on_error_code"]}',
    ]
    if sys_args['auth']:
        mitmdump_args.extend([
            '--proxyauth', sys_args["auth"],
        ])

    if sys_args['on_rst']:
        mitmdump_args.extend([
            '--set', f'on_rst',
        ])

    if sys_args['debug']:
        mitmdump_args.extend([
            '--verbose',
        ])

    if sys_args['insecure']:
        mitmdump_args.extend([
            '--ssl-insecure',
        ])
    return mitmdump(args=mitmdump_args)


addons = [
    PyMultiTor()
]

if __name__ == "__main__":
    main()
