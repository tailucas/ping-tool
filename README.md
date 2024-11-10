<a name="readme-top"></a>

[![Contributors][contributors-shield]][contributors-url]
[![Forks][forks-shield]][forks-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]
[![MIT License][license-shield]][license-url]

## About The Project

This project belongs to my open-source [collection][tailucas-url], borrowing elements from the more sophisticated [base project][baseapp-url] from which I have created a [reference implementation][simple-app-url] to generally bootstrap new projects for myself. This project has been deliberately simplified to run a Python application natively on the still-relevant [Raspberry Pi][pi-url] single-board computer (SBC). With the few remaining Model B+ devices that I have, I run a [Python][python-url] application on the Debian-based [Raspbery Pi OS][pi-os-url] with successful Python dependency compilation on ARMv6. Although this project is configured for development using VSCode [development containers][dev-container-url], I chose to install and run the application natively without running a built container due to the constrained environment of the Model B+ SBC.

## Problem

I wanted to install a backup Internet connection in my household to have ad-hoc access to the Internet during periods of service disruption on my primary, fibre connection. While my router supports a failover circuit, it would move an uncontrolled amount of traffic to a more constrained connection which also carries a consumption cap. Given that most modern clients understand the definition of a metered network connection, I decided to use a second WiFi access point (AP) which selected clients could use as a metered, backup connection. This is a simple way to control both the number of clients that use this backup circuit and the amount of data consumption. Metered connection settings are beneficial because they temporarily disable large backup or update jobs, depending on the environment and operating system.

### Constraints

Due to the data cap, nothing normally uses this backup circuit. This presents the obvious problem of regular testing of backups and the perils of [bimodal operating modes][statstab-url]. Plan A was to configure a machine on my existing network that hosts an instance of [Uptime Kuma][uptime-kuma-url] to also act as WiFi client, and maintain a constant connection to the Mobile Backup AP. I decided against this because I have a number of Internet-facing probes on the fibre network that needed to remain unchanged, including routes that [Tailscale][tailscale-url] manages for me. I needed to avoid unwanted traffic traversing the backup circuit. To manage complexity, I wanted a solution that made no changes to my existing monitoring infrastructure, other than of course configuring new probes to cover the this use-case. Instead, I wondered whether it would be possible to fashion some kind of [Layer 7 Health Check][l7-url] against the backup circuit because Uptime Kuma supports a variety of probe types, including an HTTP client test, complete with custom header support.

In summary, my requirements were:

* No changes to the machine that hosts my existing monitoring functions, other than new probes in Uptime Kuma.
* No changes to core network routing configuration, apart from existing behaviour already provided by Tailscale clients.
* No changes to *any* configuration of the Backup AP device other than to set the WiFi configuration. All interactions *must* be plug-and-play. For this reason, I picked an AP device that has both WiFi and Ethernet ports and hosts a DHCP Server that runs on both interface types. This is actually very useful for my design.

## Solution

I deliberately use IPv4 addresses here for illustration purposes of the network boundaries.

I used a 14-year old Raspberry Pi Model B+ SBC and a spare USB Ethernet dongle to create a dual-NIC device that would physically connect to both the AP device Ethernet port and a spare port on my existing home network. DHCP servers on both sides would give each interface an address on each network. By adding the SBC to my existing Tailscale "tailnet" as a [subnet router][tailscale-sr-url], I can advertise the subnet that belongs to the Backup AP to other nodes, such as the machine on my home network that runs Uptime Kuma. This allows me to build a TCP and HTTP probe that validates the liveness and health of the Backup AP itself but not the actual connection to the Internet. For obvious reasons, I *cannot* configure the SBC as an [exit node][tailscale-sr-url] because that would route all traffic through the SBC and backup Internet circuit, defeating the purpose of the boundary. This is where I built Plan B: host some kind of "deep ping" on the SBC, triggered by an HTTP probe on the interface facing the home network. Here's the final layout:

![classes](/../../../../tailucas/tailucas.github.io/blob/main/assets/ping-tool/ping-tool.drawio.png)

The magic that Tailscale provides is illustrated by the coloured, dashed flows depicted above. Probes to `192.168.0.10` are routed as normal to the interface connected to the home network. Probes to the backup AP circuit `192.168.2.1` are transparently routed through `192.168.0.10` and `192.168.2.10` so that both parts of the network are accessible, but *not* the second route to the Internet.

The machine `192.168.0.5` on the home network `192.168.0.0/24` issues an HTTP probe from Uptime Kuma to the interface on the SBC at address `192.168.0.10`. This `HTTP GET` returns success based on the criteria specified by the probe configuration (more on that in the next section). What is actually happening is that `ping-tool` is listening on port `80` (or `8080`) on `192.168.0.10`. Upon receipt of the `HTTP GET`, header configuration is read by the tool, either an ICMP *ping* or *traceroute* is issued to the destination configured in the header, using the second interface on `192.168.2.10` as a source address. Based on the out-the-box DHCP client and routing information provided by the Backup AP, packets then reach the destination on that route, delivered back to the application. After some latency and route inspection, the test result is returned to the caller still waiting on the `HTTP GET` to `192.168.0.10`.

ICMP functions are provided by the Python library [imcplib][imcplib-url], though I needed to create a [fork][icmplib-fork-url] to get the network socket to bind to the correct interface using the option `SO_BINDTODEVICE`. I tried to make the change backwards compatible and also added the useful feature of supporting an address or interface name interchangeably. I did not look deeply into why the interface was not correctly implied by the socket address because I actually wanted to use the interface name as supported natively by the Linux `ping` tool.

### Probe Configuration

`ping` configuration by issuing an `HTTP GET` request `http://192.168.0.10:8080/ping` with these headers:

```json
{
    "Host": "arbitrary.internet.host.com",
    "Source": "eth1",
    "MinLatencyMs": 10
}
```

The above configuration will trigger 3 ICMP packets to be sent to the destination host, and succeed if the minimum packet latency is 10ms as specified by the header `MinLatencyMs`. The choice of host is such that if the packets were sent over the fibre inadvertently, the latency would be much less (1-3ms). In my case, the backup circuit tends to have a latency of 15ms or more, depending on network conditions. This is a crude but seemingly robust way to infer the correct circuit.

`traceroute` configuration by issuing an `HTTP GET` request `http://192.168.0.10:8080/traceroute` with these headers:

```json
{
    "Host": "arbitrary.internet.host.com",
    "Source": "eth1",
    "MinLatencyMs": 10,
    "RouteIncludeCsv": "192.168.2.1",
    "RouteExcludeCsv": "192.168.0.1",
    "HopsMustIncludeOrg": "AS12345",
    "HopsMustExcludeOrg": "AS54321"
}
```

This configuration will trigger a `traceroute` to the destination, and for the destination hop enforce a minimum expected latency as with the ping test above using the header `MinLatencyMs`. `traceroute` offers additional information to validate the route. `RouteIncludeCsv` ensures that the route always contains the correct default gateway, and `RouteExcludeCsv` ensures that the fibre network gateway is not present in the route. `HopsMustIncludeOrg` ensures that the route must include the organization that owns the network (in this case the Mobile Network ISP) as specified by [IPinfo][ipinfo-url]. `HopsMustExcludeOrg` is the negative test, ensuring that the fibre network provider is not on the route, which obviously also influences the choice of the destination host. Luckily there's plenty out there.

<p align="right">(<a href="#readme-top">back to top</a>)</p>

### Built With

Technologies that help make this package useful:

[![Poetry][poetry-shield]][poetry-url]
[![Python][python-shield]][python-url]
[![Uptime Kuma][uptime-kuma-shield]][uptime-kuma-url]
[![Tailscale][tailscale-shield]][tailscale-url]
[![IPinfo][ipinfo-shield]][ipinfo-url]

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- GETTING STARTED -->
## Getting Started

Here is some detail about the intended use of this package.

### Environment Setup

As stated above, while this application can run as a container, I have decided to run it natively. Here's an excerpt of my SBC setup based on the [Raspberry Pi OS][pi-os-url] based on Debian. Unfortunately, [task](https://taskfile.dev/installation/#install-script) does not appear to support ARMv6 which is what I normally use to orchestrate project setup and container builds. The steps below bypass this.

* `poetry` for Python dependency management: https://python-poetry.org/docs/#installation
* `python3` for Python runtime: https://www.python.org/downloads/ which appears to be included now as default on Raspberry Pi OS.

TL;DR: here are the steps I followed (obviously defer to the package docs to keep this valid) to get the dependency closure to install correctly on `armv6l`. Poetry needs the Python `cryptography` wheel (as do most things), Rust is needed to build the `cryptography` wheel (there's a long debate on this not in the scope of this guide), and the Python3 development headers are needed to build against the Python programming interface.

```sh
sudo apt install python3-dev
sudo apt install libssl-dev
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | bash -s -- -y
curl -sSL https://install.python-poetry.org | python3 -
```

**Note:** This took a *very* long time on my model B+. I didn't keep tabs but probably 6 hours in total.

### Installation

1. Clone the repo.
```sh
git clone https://github.com/tailucas/ping-tool.git
cd ping-tool
```
2. Create a `.env` file for your needs in that directory, and add `IPINFO_TOKEN` if you want to enforce organization checks on the route.
```sh
DEVICE_NAME="ping-tool"
SERVER_ADDRESS="0.0.0.0"
SERVER_PORT="8080"
APP_NAME="ping-tool"
```
3. Tell Poetry to create the Python virtual environment (configured already to do this in the project directory) and install the application.
```sh
poetry install
```
4. To run and auto-start the application, I created a systemd unit file. A sample is included in this project so make the required edits. Note that elevated privileges are needed by `icmplib` in order to perform `traceroute` functions.
```sh
sudo cp ping-tool.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable ping-tool
sudo systemctl start ping-tool
sudo systemctl status ping-tool
journalctl -f -u ping-tool
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>

<!-- USAGE EXAMPLES -->
## Usage

Now configure the HTTP probes according to the configuration above. Here's some examples using the `curl` command:

```sh
curl -v -H "Host: arbitrary.internet.host.com" -H "MinLatencyMs: 10" -H "Source: eth1" http://localhost:8080/ping
```

```sh
curl -v -H "Host: arbitrary.internet.host.com" -H "MinLatencyMs: 10" -H "Source: eth1" -H "RouteIncludeCsv: 192.168.2.1" -H "RouteExcludeCsv: 192.168.0.1" -H "HopsMustIncludeOrg: AS12345" -H "HopsMustExcludeOrg: AS54321" http://localhost:8080/traceroute
```

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- LICENSE -->
## License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.

<p align="right">(<a href="#readme-top">back to top</a>)</p>


<!-- ACKNOWLEDGMENTS -->
## Acknowledgments

* [Template on which this README is based](https://github.com/othneildrew/Best-README-Template)
* [All the Shields](https://github.com/progfay/shields-with-icon)

<p align="right">(<a href="#readme-top">back to top</a>)</p>

[![Hits](https://hits.seeyoufarm.com/api/count/incr/badge.svg?url=https%3A%2F%2Fgithub.com%2Ftailucas%2Fping-tool%2F&count_bg=%2379C83D&title_bg=%23555555&icon=&icon_color=%23E7E7E7&title=visits&edge_flat=true)](https://hits.seeyoufarm.com)

<!-- MARKDOWN LINKS & IMAGES -->
<!-- https://www.markdownguide.org/basic-syntax/#reference-style-links -->
[contributors-shield]: https://img.shields.io/github/contributors/tailucas/ping-tool.svg?style=for-the-badge
[contributors-url]: https://github.com/tailucas/ping-tool/graphs/contributors
[forks-shield]: https://img.shields.io/github/forks/tailucas/ping-tool.svg?style=for-the-badge
[forks-url]: https://github.com/tailucas/ping-tool/network/members
[stars-shield]: https://img.shields.io/github/stars/tailucas/ping-tool.svg?style=for-the-badge
[stars-url]: https://github.com/tailucas/ping-tool/stargazers
[issues-shield]: https://img.shields.io/github/issues/tailucas/ping-tool.svg?style=for-the-badge
[issues-url]: https://github.com/tailucas/ping-tool/issues
[license-shield]: https://img.shields.io/github/license/tailucas/ping-tool.svg?style=for-the-badge
[license-url]: https://github.com/tailucas/ping-tool/blob/master/LICENSE

[imcplib-shield]: https://img.shields.io/github/license/tailucas/ping-tool.svg?style=for-the-badge
[imcplib-url]: https://github.com/ValentinBELYN/icmplib
[poetry-url]: https://python-poetry.org/
[poetry-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Poetry&color=60A5FA&logo=Poetry&logoColor=FFFFFF&label=
[python-url]: https://www.python.org/
[python-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Python&color=3776AB&logo=Python&logoColor=FFFFFF&label=
[requests-url]: https://requests.readthedocs.io/en/latest/
[requests-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Requests&color=60A5FA&logo=Requests&logoColor=FFFFFF&label=
[tailscale-url]: https://tailscale.com/
[tailscale-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Tailscale&color=60A5FA&logo=Tailscale&logoColor=FFFFFF&label=
[uptime-kuma-url]: https://uptime.kuma.pet/
[uptime-kuma-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=Uptime%20Kuma&color=60A5FA&logo=Uptime%20Kuma&logoColor=FFFFFF&label=
[ipinfo-url]: https://ipinfo.io/
[ipinfo-shield]: https://img.shields.io/static/v1?style=for-the-badge&message=IPinfo&color=60A5FA&logo=IPinfo&logoColor=FFFFFF&label=

[tailucas-url]: https://github.com/tailucas
[baseapp-url]: https://github.com/tailucas/base-app
[simple-app-url]: https://github.com/tailucas/simple-app
[dev-container-url]: https://code.visualstudio.com/docs/devcontainers/containers
[icmplib-fork-url]: https://github.com/tglucas/icmplib/commit/83a7151bd910485fb8d73511ab69ed3576ab4d21#diff-7e8617a303da9de7a49dbbcb38738ea1b5ad2442d97f516e3d67da8325f603ffR98
[pi-url]: https://en.wikipedia.org/wiki/Raspberry_Pi
[pi-os-url]: https://www.raspberrypi.com/software/
[statstab-url]: https://docs.aws.amazon.com/wellarchitected/latest/reliability-pillar/rel_withstand_component_failures_static_stability.html
[l7-url]: https://kemptechnologies.com/load-balancer/layer-7-load-balancing
[tailscale-sr-url]: https://tailscale.com/kb/1019/subnets
[tailscale-en-url]: https://tailscale.com/kb/1103/exit-nodes
