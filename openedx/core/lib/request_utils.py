""" Utility functions related to HTTP requests """

import logging
import re

import crum
from django.conf import settings
from django.test.client import RequestFactory
from django.utils.deprecation import MiddlewareMixin
from edx_django_utils.monitoring import set_custom_attribute
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from six.moves.urllib.parse import urlparse

from edx_toggles.toggles import LegacyWaffleFlag, LegacyWaffleFlagNamespace
from openedx.core.djangoapps.site_configuration import helpers as configuration_helpers

# accommodates course api urls, excluding any course api routes that do not fall under v*/courses, such as v1/blocks.
COURSE_REGEX = re.compile(r'^(.*?/courses/)(?!v[0-9]+/[^/]+){}'.format(settings.COURSE_ID_PATTERN))

WAFFLE_FLAG_NAMESPACE = LegacyWaffleFlagNamespace(name='request_utils')
CAPTURE_COOKIE_SIZES = LegacyWaffleFlag(WAFFLE_FLAG_NAMESPACE, 'capture_cookie_sizes', __name__)
log = logging.getLogger(__name__)


def get_request_or_stub():
    """
    Return the current request or a stub request.

    If called outside the context of a request, construct a fake
    request that can be used to build an absolute URI.

    This is useful in cases where we need to pass in a request object
    but don't have an active request (for example, in tests, celery tasks, and XBlocks).
    """
    request = crum.get_current_request()

    if request is None:

        # The settings SITE_NAME may contain a port number, so we need to
        # parse the full URL.
        full_url = "http://{site_name}".format(site_name=settings.SITE_NAME)
        parsed_url = urlparse(full_url)

        # Construct the fake request.  This can be used to construct absolute
        # URIs to other paths.
        return RequestFactory(
            SERVER_NAME=parsed_url.hostname,
            SERVER_PORT=parsed_url.port or 80,
        ).get("/")

    else:
        return request


def safe_get_host(request):
    """
    Get the host name for this request, as safely as possible.

    If ALLOWED_HOSTS is properly set, this calls request.get_host;
    otherwise, this returns whatever settings.SITE_NAME is set to.

    This ensures we will never accept an untrusted value of get_host()
    """
    if isinstance(settings.ALLOWED_HOSTS, (list, tuple)) and '*' not in settings.ALLOWED_HOSTS:
        return request.get_host()
    else:
        return configuration_helpers.get_value('site_domain', settings.SITE_NAME)


def course_id_from_url(url):
    """
    Extracts the course_id from the given `url`.
    """
    if not url:
        return None

    match = COURSE_REGEX.match(url)

    if match is None:
        return None

    course_id = match.group('course_id')

    if course_id is None:
        return None

    try:
        return CourseKey.from_string(course_id)
    except InvalidKeyError:
        return None


class CookieMonitoringMiddleware(MiddlewareMixin):
    """
    Middleware for monitoring the size and growth of all our cookies, to see if
    we're running into browser limits.
    """
    def process_request(self, request):
        """
        Emit custom attributes for cookie size values for every cookie we have.

        Don't log contents of cookies because that might cause a security issue.
        We just want to see if any cookies are growing out of control.

        A useful NRQL Query:
            SELECT count(*), max(`cookies.max.group.size`) from Transaction FACET
            `cookies.max.group.name`

            SELECT * FROM Transaction WHERE cookies_total_size > 6000

        Attributes that are added by this middleware:

        cookies.<cookie_name>.size: The size of a cookie by the given name.
        cookies.<group_prefix>.group.size: The size of a group of cookies. For example
            the sum of the size of all braze cookies would be the value of the
            `cookies.ab.group.size` attribute.
        cookies.max.name: The name of the largest cookie sent by the user.
        cookies.max.size: The size of the largest cookie sent by the user.
        cookies.max.group.name: The name of the largest group of cookies. A single cookie
            counts as a group of one for this calculation.
        cookies.max.group.size: The sum total size of all the cookies in the largest group.
        cookies_total_size: The sum total size of all cookies in this request.

        """
        if not CAPTURE_COOKIE_SIZES.is_enabled():
            return

        cookie_names_to_size = {}
        cookie_groups_to_size = {}

        for name, value in request.COOKIES.items():
            # Get cookie size for all cookies.
            cookie_size = len(value)
            cookie_names_to_size[name] = cookie_size

            # Group cookies by their prefix seperated by a period or underscore
            grouping_name = re.split('[._]', name, 1)[0]
            if grouping_name and grouping_name != name:
                # Add or update the size for this group.
                cookie_groups_to_size[grouping_name] = cookie_groups_to_size.get(grouping_name, 0) + cookie_size

        max_cookie_name = max(cookie_names_to_size, key=lambda name: cookie_names_to_size[name])
        max_cookie_size = cookie_names_to_size[max_cookie_name]

        max_group_cookie_name = max(cookie_groups_to_size, key=lambda name: cookie_groups_to_size[name])
        max_group_cookie_size = cookie_groups_to_size[max_group_cookie_name]

        # If a single cookies is bigger than any group of cookies, we want max_group... to reflect that.
        # Treating an individual cookie as a group of 1 for calculating the max.
        if max_group_cookie_size < max_cookie_size:
            max_group_cookie_name = max_cookie_name
            max_group_cookie_size = max_cookie_size

        # Only log the groups because adding an arbitrary number of individual cookies pushes too many
        # metrics into NR and results in other metrics getting dropped potentially.
        for name, size in cookie_groups_to_size.items():
            attribute_name = 'cookies.{}.group.size'.format(name)
            set_custom_attribute(attribute_name, size)
            log.debug(u'%s = %d', attribute_name, size)

        set_custom_attribute('cookies.max.name', max_cookie_name)
        set_custom_attribute('cookies.max.size', max_cookie_size)
        set_custom_attribute('cookies.max.group.name', max_group_cookie_name)
        set_custom_attribute('cookies.max.group.size', max_group_cookie_size)

        total_cookie_size = sum(cookie_names_to_size.values())
        set_custom_attribute('cookies_total_size', total_cookie_size)
        log.debug(u'cookies_total_size = %d', total_cookie_size)
