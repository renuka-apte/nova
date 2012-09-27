=====================================
OpenStack Nova Testing Infrastructure
=====================================

A note of clarification is in order, to help those who are new to testing in
OpenStack nova:

- actual unit tests are created in the "tests" directory;
- the "testing" directory is used to house the infrastructure needed to support
  testing in OpenStack Nova.

This README file attempts to provide current and prospective contributors with
everything they need to know in order to start creating unit tests and
utilizing the convenience code provided in nova.testing.

Note: the content for the rest of this file will be added as the work items in
the following blueprint are completed:
  https://blueprints.launchpad.net/nova/+spec/consolidate-testing-infrastructure


Test Types: Unit vs. Functional vs. Integration
-----------------------------------------------

TBD

Writing Unit Tests
------------------

TBD

Using Fakes
~~~~~~~~~~~

TBD

test.TestCase
-------------
The TestCase class from nova.test (generally imported as test) will
automatically manage self.stubs using the stubout module and self.mox
using the mox module during the setUp step. They will automatically
verify and clean up during the tearDown step.

If using test.TestCase, calling the super class setUp is required and
calling the super class tearDown is required to be last if tearDown
is overriden.

Writing Functional Tests
------------------------

TBD

Writing Integration Tests
-------------------------

TBD

Tests and Exceptions
--------------------
A properly written test asserts that particular behavior occurs. This can
be a success condition or a failure condition, including an exception.
When asserting that a particular exception is raised, the most specific
exception possible should be used.

In particular, testing for Exception being raised is almost always a
mistake since it will match (almost) every exception, even those
unrelated to the exception intended to be tested.

This applies to catching exceptions manually with a try/except block,
or using assertRaises().

Example::

    self.assertRaises(exception.InstanceNotFound, db.instance_get_by_uuid,
                      elevated, instance_uuid)

If a stubbed function/method needs a generic exception for testing
purposes, test.TestingException is available.

Example::

    def stubbed_method(self):
        raise test.TestingException()
    self.stubs.Set(cls, 'inner_method', stubbed_method)

    obj = cls()
    self.assertRaises(test.TestingException, obj.outer_method)
