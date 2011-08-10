from datetime import datetime

from django.conf import settings
from django.db import models

from django.contrib.auth.models import User
from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes import generic
from django.utils.translation import ugettext_lazy as _, ungettext

from flag.settings import LIMIT_SAME_OBJECT_FOR_USER, LIMIT_FOR_OBJECT
from flag import signals


STATUS = getattr(settings, "FLAG_STATUSES", [
    ("1", _("flagged")),
    ("2", _("flag rejected by moderator")),
    ("3", _("creator notified")),
    ("4", _("content removed by creator")),
    ("5", _("content removed by moderator")),
])


class FlagException(Exception):
    """
    Base class for django-flag exceptions
    """
    pass

class ContentAlreadyFlaggedByUserException(FlagException):
    """
    Exception raised when a user try to flag an object he had
    already flagged and the number of its flags raised the
    LIMIT_SAME_OBJECT_FOR_USER value
    """
    pass

class ContentFlaggedEnoughException(FlagException):
    """
    Exception raised when someone try to flag an object which is
    already flagged and the LIMIT_FOR_OBJECT is raised
    """
    pass

class FlaggedContentManager(models.Manager):
    """
    Manager for the FlaggedContent models
    """

    def get_for_object(self, content_object):
        """
        Helper to get a FlaggedContent instance for the given object
        """
        content_type = ContentType.objects.get_for_model(content_object)
        return self.get(
                content_type__id=content_type.id,
                object_id=content_object.id
            )


class FlaggedContent(models.Model):

    content_type = models.ForeignKey(ContentType)
    object_id = models.PositiveIntegerField()
    content_object = generic.GenericForeignKey("content_type", "object_id")

    creator = models.ForeignKey(User, related_name="flagged_content", null=True, blank=True) # user who created flagged content -- this is kept in model so it outlives content
    status = models.CharField(max_length=1, choices=STATUS, default="1")
    moderator = models.ForeignKey(User, null=True, related_name="moderated_content") # moderator responsible for last status change
    count = models.PositiveIntegerField(default=1)

    # manager
    objects = FlaggedContentManager()

    class Meta:
        unique_together = [("content_type", "object_id")]

    def count_flags_by_user(self, user):
        """
        Helper to get the number of flags on this flagged content by the
        given user
        """
        return self.flaginstance_set.filter(user=user).count()

    def can_be_flagged(self):
        """
        Check that the LIMIT_FOR_OBJECT is not raised
        """
        if not LIMIT_FOR_OBJECT:
            return True
        return self.count < LIMIT_FOR_OBJECT

    def assert_can_be_flagged(self):
        """
        Raise an acception if the "can_be_flagged" method return False
        """
        if not self.can_be_flagged():
            raise ContentFlaggedEnoughException(_('Flag limit raised'))
        return True

    def can_be_flagged_by_user(self, user):
        """
        Check that the LIMIT_SAME_OBJECT_FOR_USER is not raised for this user
        """
        if not LIMIT_SAME_OBJECT_FOR_USER:
            return True
        if not self.can_be_flagged():
            return False
        return self.count_flags_by_user(user) < LIMIT_SAME_OBJECT_FOR_USER

    def assert_can_be_flagged_by_user(self, user):
        """
        Raise an exception if the given user cannot flag this object
        """
        try:
            self.assert_can_be_flagged()
        except ContentFlaggedEnoughException, e:
            raise e
        else:
            # do not use self.can_be_flagged_by_user because we need the count
            count = self.count_flags_by_user(user)
            if count >= LIMIT_SAME_OBJECT_FOR_USER:
                error = ungettext(
                            'You already flagged this',
                            'You already flagged this %(count)d times',
                            count
                        ) % {
                            'count': count
                        }
                raise ContentAlreadyFlaggedByUserException(error)

        return True



class FlagInstance(models.Model):

    flagged_content = models.ForeignKey(FlaggedContent)
    user = models.ForeignKey(User) # user flagging the content
    when_added = models.DateTimeField(default=datetime.now)
    when_recalled = models.DateTimeField(null=True) # if recalled at all
    comment = models.TextField(null=True, blank=True) # comment by the flagger


def add_flag(flagger, content_type, object_id, content_creator, comment, status=None):

    # check if it's already been flagged
    defaults = dict(creator=content_creator)
    if status is not None:
        defaults["status"] = status
    flagged_content, created = FlaggedContent.objects.get_or_create(
        content_type = content_type,
        object_id = object_id,
        defaults = defaults
    )

    # check if the current user can flag this object
    flagged_content.assert_can_be_flagged_by_user(flagger)

    if not created:
        flagged_content.count = models.F("count") + 1
        flagged_content.save()
        # pull flagged_content from database to get count attribute filled
        # properly (not the best way, but works)
        flagged_content = FlaggedContent.objects.get(pk=flagged_content.pk)

    flag_instance = FlagInstance(
        flagged_content = flagged_content,
        user = flagger,
        comment = comment
    )
    flag_instance.save()

    signals.content_flagged.send(
        sender = FlaggedContent,
        flagged_content = flagged_content,
        flagged_instance = flag_instance,
    )

    return flag_instance
