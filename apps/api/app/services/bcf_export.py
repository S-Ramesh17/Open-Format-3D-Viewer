import io
import uuid
import zipfile
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, tostring

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundException
from app.models.annotation import Annotation
from app.models.annotation_comment import AnnotationComment
from app.models.model import Model


async def export_bcf(model_id: uuid.UUID, db: AsyncSession) -> bytes:
    """
    Export all annotations on a model as a BCF 2.1 ZIP archive.
    BCF spec: each topic is a folder named by GUID containing
    markup.bcf (XML) + optional viewpoint.bcfv. bcf.version at archive root.
    """
    result = await db.execute(select(Model).where(Model.id == model_id))
    model = result.scalar_one_or_none()
    if not model:
        raise NotFoundException("Model not found")

    result = await db.execute(
        select(Annotation).where(Annotation.model_id == model_id)
    )
    annotations = result.scalars().all()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bcf.version", '<?xml version="1.0"?>\n<Version VersionId="2.1"/>')

        for annotation in annotations:
            topic_guid = str(annotation.id)

            result = await db.execute(
                select(AnnotationComment)
                .where(AnnotationComment.annotation_id == annotation.id)
                .order_by(AnnotationComment.created_at.asc())
            )
            comments = result.scalars().all()

            markup_xml = _build_markup_xml(annotation, comments)
            zf.writestr(f"{topic_guid}/markup.bcf", markup_xml)

    buffer.seek(0)
    return buffer.read()


def _build_markup_xml(annotation: Annotation, comments: list[AnnotationComment]) -> str:
    root = Element("Markup")

    topic = SubElement(root, "Topic", {
        "Guid": str(annotation.id),
        "TopicType": "Issue",
        "TopicStatus": "Closed" if annotation.status == "resolved" else "Open",
    })
    title_el = SubElement(topic, "Title")
    title_el.text = annotation.title

    if annotation.body:
        desc_el = SubElement(topic, "Description")
        desc_el.text = annotation.body

    creation_date = SubElement(topic, "CreationDate")
    creation_date.text = annotation.created_at.isoformat()

    for comment in comments:
        comment_el = SubElement(root, "Comment", {"Guid": str(comment.id)})
        date_el = SubElement(comment_el, "Date")
        date_el.text = comment.created_at.isoformat()
        comment_text_el = SubElement(comment_el, "Comment")
        comment_text_el.text = comment.body
        topic_ref = SubElement(comment_el, "Topic", {"Guid": str(annotation.id)})

    xml_bytes = tostring(root, encoding="utf-8", xml_declaration=True)
    return xml_bytes.decode("utf-8")