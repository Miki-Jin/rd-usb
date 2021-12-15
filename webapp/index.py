import csv
import io
from math import ceil, floor
import os
from time import time
import traceback

from flask import url_for, request, jsonify, redirect, flash, make_response
from flask.blueprints import Blueprint
from flask.templating import render_template
import pendulum

from interfaces.tc import TcSerialInterface, TcBleInterface
from utils.config import Config, static_path
from utils.formatting import Format
from utils.storage import Storage
from utils.version import version


class Index:
    config = None
    storage = None
    import_in_progress = False

    def register(self):
        blueprint = Blueprint("index", __name__, template_folder="templates")
        blueprint.add_url_rule("/", "default", self.render_default)
        blueprint.add_url_rule("/data", "data", self.render_data)
        blueprint.add_url_rule("/graph", "graph", self.render_graph)
        blueprint.add_url_rule("/graph.json", "graph_data", self.render_graph_data)
        blueprint.add_url_rule("/ble", "ble", self.render_ble)
        blueprint.add_url_rule("/serial", "serial", self.render_serial)
        blueprint.add_url_rule("/tc66c-import", "tc66c_import", self.render_tc66c_import, methods=["GET", "POST"])
        blueprint.context_processor(self.fill)
        return blueprint

    def init(self):
        self.config = Config()
        self.storage = Storage()

    def fill(self):
        variables = {
            "rd_user_version": version,
            "format": Format(),
            "url_for": self.url_for,
            "version": self.config.read("version", "UM34C"),
            "port": self.config.read("port", ""),
            "rate": str(self.config.read("rate", 1.0)),
            "name": self.config.read("name", pendulum.now().format("YYYY-MM-DD")),
            "ble_address": self.config.read("ble_address"),
        }

        status = self.storage.fetch_status()
        variables["status"] = status.title()
        variables["connect_disabled"] = status != "disconnected"
        variables["connect_button"] = "Connect" if status == "disconnected" else "Disconnect"

        return variables

    def render_default(self):
        self.init()
        self.storage.clear_log()
        log = self.storage.fetch_log()
        return render_template("default.html", log=log, page="default")

    def render_data(self):
        self.init()

        names, selected = self.prepare_selection()
        name = self.storage.translate_selected_name(selected)

        if request.args.get("export") == "":
            string = io.StringIO()
            writer = csv.writer(string)
            format = Format()

            names = []
            for field in format.export_fields:
                names.append(format.field_name(field))
            writer.writerow(names)

            run_time_offset = None
            for item in self.storage.fetch_measurements(name):
                if run_time_offset is None and item["resistance"] < 9999.9:
                    run_time_offset = item["timestamp"]

                rune_time = 0
                if run_time_offset is not None:
                    rune_time = round(item["timestamp"] - run_time_offset)

                values = []
                for field in format.export_fields:
                    if field == "time":
                        values.append(format.time(item))
                    elif field == "run_time":
                        remaining = rune_time
                        hours = floor(remaining / 3600)
                        remaining -= hours * 3600
                        minutes = floor(remaining / 60)
                        remaining -= minutes * 60
                        seconds = remaining
                        parts = [
                            hours,
                            minutes,
                            seconds,
                        ]
                        for index, value in enumerate(parts):
                            parts[index] = str(value).zfill(2)
                        values.append(":".join(parts))
                    elif field == "run_time_seconds":
                        values.append(rune_time)
                    else:
                        values.append(item[field])
                writer.writerow(values)

            output = make_response(string.getvalue())
            output.headers["Content-Disposition"] = "attachment; filename=" + name + ".csv"
            output.headers["Content-type"] = "text/csv"
            return output

        elif request.args.get("destroy") == "":
            self.storage.destroy_measurements(name)
            flash("Measurements with session name '" + name + "' were deleted", "danger")
            return redirect(request.path)

        page = request.args.get("page", 1, int)
        limit = 100
        offset = limit * (page - 1)
        count = self.storage.fetch_measurements_count(name)
        pages = self.prepare_pages(name, page, limit, count)

        measurements = self.storage.fetch_measurements(name, limit, offset)

        return render_template(
            "data.html",
            names=names,
            selected=selected,
            measurements=measurements,
            page="data",
            pages=pages,
        )

    def prepare_pages(self, name, page, limit, count, blocks=10):
        first_page = 1
        related = 3
        last_page = int(ceil(count / limit))
        steps = set(range(max((first_page, page - related)), min((last_page, page + related)) + 1))
        quotient = (last_page - 1) / blocks
        if len(steps) > 1:
            for index in range(0, blocks):
                steps.add(round(quotient * index) + first_page)
        steps.add(last_page)
        steps = sorted(steps)

        pages = []
        for number in steps:
            pages.append({
                "number": number,
                "link": url_for("index.data", page=number, name=name),
                "current": number == page,
            })

        return pages

    def render_graph(self):
        self.init()

        names, selected = self.prepare_selection()

        last_measurement = None
        if selected == "":
            last_measurement = self.storage.fetch_last_measurement()

        return render_template(
            "graph.html",
            names=names,
            selected=selected,
            item=last_measurement,
            left_axis="voltage",
            right_axis="current",
            colors=self.config.read("colors", "colorful"),
            page="graph"
        )

    def render_graph_data(self):
        self.init()

        selected = request.args.get("name")
        name = self.storage.translate_selected_name(selected)

        left_axis = request.args.get("left_axis")
        right_axis = request.args.get("right_axis")
        colors = request.args.get("colors")
        if self.config.read("colors") != colors:
            self.config.write("colors", colors, flush=True)

        format = Format()

        data = []
        for item in self.storage.fetch_measurements(name):
            if left_axis in item:
                data.append({
                    "date": format.timestamp(item),
                    "left": item[left_axis],
                    "right": item[right_axis],
                })

        return jsonify(data)

    def prepare_selection(self):
        names = self.storage.fetch_measurement_names()
        selected = request.args.get("name")
        if not selected:
            selected = ""

        return names, selected

    def fill_config_from_parameters(self):
        value = request.args.get("version")
        if value is not None:
            self.config.write("version", value)

        value = request.args.get("name")
        if value is not None:
            self.config.write("name", value)

        value = request.args.get("rate")
        if value is not None:
            self.config.write("rate", float(value))

    def render_ble(self):
        self.init()
        self.fill_config_from_parameters()
        return render_template(
            "ble.html"
        )

    def render_serial(self):
        self.init()
        self.fill_config_from_parameters()
        return render_template(
            "serial.html"
        )

    def render_tc66c_import(self):
        self.init()
        self.fill_config_from_parameters()

        message = None
        if self.config.read("version") not in ["TC66C-USB"]:
            message = "Available only for TC66C USB"
        elif self.storage.fetch_status() != "disconnected":
            message = "Disconnect first"

        if "do" in request.form:
            if message is None:
                name = request.form.get("session_name")
                if not name:
                    message = "Please provide session name"
                else:
                    message = self.do_tc66c_import(name)
                    if message is None:
                        return redirect(url_for("index.graph"))

        return render_template(
            "tc66c-import.html",
            message=message,
            session_name="TC66C recording %s" % pendulum.now().format("YYYY-MM-DD hh:mm:ss")
        )

    def do_tc66c_import(self, name):
        message = None
        if self.import_in_progress:
            return "Import is already running"
        self.import_in_progress = True
        serial_timeout = int(self.config.read("serial_timeout", 10))
        interface = TcSerialInterface(self.config.read("port"), serial_timeout)
        self.storage.fetch_status()
        try:
            interface.connect()
            begin = time()
            for index, record in enumerate(interface.read_records()):
                data = {
                    "name": name,
                    "timestamp": begin + index,
                    "voltage": record["voltage"],
                    "current": record["current"],
                    "power": 0,
                    "temperature": 0,
                    "data_plus": 0,
                    "data_minus": 0,
                    "mode_id": 0,
                    "mode_name": None,
                    "accumulated_current": 0,
                    "accumulated_power": 0,
                    "accumulated_time": 0,
                    "resistance": 0,
                }
                self.storage.store_measurement(data)

        except (KeyboardInterrupt, SystemExit):
            raise
        except:
            message = "Failed to connect:"
            exception = traceback.format_exc()
            self.storage.log(exception)
            message += "\n%s" % exception
        finally:
            interface.disconnect()
            self.import_in_progress = False

        return message

    def url_for(self, endpoint, **values):
        if endpoint == "static":
            filename = values.get("filename", None)
            if filename:
                file_path = static_path + "/" + filename
                values["v"] = int(os.stat(file_path).st_mtime)
        return url_for(endpoint, **values)
