#!/usr/bin/python3
from argparse import ArgumentParser
import collections
from collections import defaultdict
import datetime
import json
from launchpadlib.launchpad import Launchpad
import logging
import os
from string import Template
import sys
import yaml
from yaml.representer import Representer


UTCNOW = datetime.datetime.now()
CACHEDIR = os.path.expanduser("~/.launchpadlib/cache")

yaml.add_representer(collections.defaultdict, Representer.represent_dict)


class Project(object):
    """Wrapper for a launchpad project"""

    def __init__(self, launchpad, project: str, window: int):
        self.project = launchpad.projects[project]
        self.window = window
        self.review_status = [
            "Needs review",
            "Approved",
            "Rejected",
            "Merged",
        ]
        self.merge_proposals = self._render_merge_proposals()

    @property
    def name(self):
        return self.project.name

    def _render_merge_proposals(self) -> list:
        """Fetch all merge proposals for the project and filter by the 
        specified window.
        :returns: list of launchpad.branch_merge_proposal objects
        """
        project_mps = []
        logging.debug(
            f"Fetching votes for {self.project.name} merge proposals. "
            "This may take a while."
        )
        for status in self.review_status:
            mps = self.project.getMergeProposals(status=status)
            for mp in mps:
                if in_window(self.window, mp.date_created):
                    project_mps.append(mp)
        return project_mps

    def render_project_votes_by_user(self, user) -> dict:
        """Render all votes on a given project for a given user.
        :param user: launchpad.person object
        :returns: dict of { mp link: vote }
        """
        votes = {}
        for mp in self.merge_proposals:
            for vote in [v for v in mp.votes if v.comment]:
                if vote.reviewer.display_name == user.display_name:
                    votes[mp.web_link] = vote.comment.vote
        return votes


class Report(object):
    """Build an activity report for a specified Launchpad user."""

    def __init__(self, launchpad, user: str, projects: list, window: int):
        self.launchpad = launchpad
        self.user = self.launchpad.people[user]
        self.projects = projects
        self.window = window
        self.since = UTCNOW - datetime.timedelta(window)
        self.status = [
            "New",
            "Incomplete",
            "Invalid",
            "Won't Fix",
            "Confirmed",
            "Triaged",
            "In Progress",
            "Fix Committed",
            "Fix Released",
        ]

    def _render_reported(self) -> dict:
        """Fetch and render bugs reported by the user.
        :returns: dict of { project: { bug info } } 
        """
        logging.debug(f"Fetching reported bugs for {self.user.display_name}")
        reported = defaultdict(list)
        tasks = self.user.searchTasks(
            bug_reporter=self.user, status=self.status, created_since=self.since
        )
        tasks = [LPWrap(t) for t in tasks]
        for t in tasks:
            if in_window(self.window, t.bug.date_created):
                reported[t.bug_target_name].append(
                    {t.bug.id: t.title,}
                )
        return reported

    def _render_merge_proposals(self) -> defaultdict:
        """Renders merge proposals submitted by a given user.
        :returns: dict of { project: [ mp links ] }
        """
        logging.debug(f"Fetching merge proposals for {self.user.display_name}")
        proposals = defaultdict(list)
        for status in [
            "Work in progress",
            "Needs review",
            "Approved",
            "Rejected",
            "Merged",
            "Code failed to merge",
            "Queued",
            "Superseded",
        ]:
            all_mps = self.user.getMergeProposals(status=status)
            for mp in all_mps:
                if not in_window(self.window, mp.date_created):
                    break
                project = mp.web_link.split("/")[4]
                proposals[project].append(mp.web_link)
        return proposals

    def generate(self) -> dict:
        """Build the full report of a user.
        :returns: dict of data on a given user
        """
        user_data = {
            "merge_proposals": self._render_merge_proposals(),
            "bug_reports": self._render_reported(),
            "code_reviews": {},
        }
        for project in self.projects:
            user_data["code_reviews"][
                project.name
            ] = project.render_project_votes_by_user(self.user)

        return user_data


class LPWrap:
    """Simple wrapper Cache-Proxy for LP objects"""

    def __init__(self, lpObj):
        self.lpObj = lpObj

    def __getattr__(self, attr):
        result = getattr(self.lpObj, attr)
        # Tricky. We look at the attr, and if it is another launchpadlib
        # object, then we wrap *it* too. e.g., you asked for task.bug, where
        # bug is a launchpadlib object contained within the task lplib object.
        # Eventually, we'll just get a normal attribute that isn't an lplib
        # object, which we cache with setattr and then later retrieve with
        # the above getattr.
        if result.__class__.__name__ == "Entry":
            result = LPWrap(result)
        setattr(self, attr, result)
        return result


def in_window(window, date):
    """Check if the requested date is within a window of time
    :returns: True if date is within window
    """
    win = datetime.timedelta(window)
    if date == None:
        return False
    date = date.replace(tzinfo=None)
    delta = UTCNOW - date
    return delta <= win


def output_data(report, format="yaml"):
    """Outputs report data in the specified format."""
    format = format.lower()
    if format == "yaml":
        return yaml.dump(report, default_flow_style=False)
    elif format == "json":
        return json.dumps(report)
    else:
        raise NotImplementedError


def parse_args(args):
    parser = ArgumentParser()
    parser.add_argument(
        "-u", "--users", required=True, help="Specify the LP users separted by commas",
    )
    parser.add_argument(
        "-p",
        "--projects",
        required=True,
        help="Specify the LP projects separted by commas",
    )
    parser.add_argument(
        "-w",
        "--window",
        type=int,
        default=8,
        help="Window of days to query. Defaults to 8",
    )
    parser.add_argument(
        "--debug",
        action="store_const",
        const=logging.DEBUG,
        default=logging.INFO,
        help="Enable debug output",
    )
    parser.add_argument(
        "--format",
        "-f",
        default="yaml",
        choices=["YAML", "yaml", "JSON", "json"],
        help="Output format. Choose from YAML(default), JSON, CSV",
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Quiet mode, skips printing."
    )
    parser.add_argument(
        "--out", "-o", help="Optional, output to a file. Will overwrite, use carefully."
    )
    return parser.parse_args(args)


def main(args):
    opts = parse_args(args)
    logging.basicConfig(format="%(asctime)s - %(message)s", level=opts.debug)
    launchpad = Launchpad.login_with(
        "contrib-tracker", "production", CACHEDIR, version="devel"
    )

    projects = []
    for project in opts.projects.split(","):
        projects.append(Project(launchpad, project, opts.window))

    reports = {}
    for user in opts.users.split(","):
        report = Report(launchpad, user, projects, opts.window)
        reports[user] = report.generate()

    if not opts.quiet:
        print(output_data(reports, format=opts.format))

    if opts.out:
        with open(opts.out, "w+") as f:
            f.write(output_data(reports, format=opts.format))


if __name__ == "__main__":
    main(sys.argv[1:])
